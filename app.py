"""
app.py – Oralify v2 Flask application.
"""

from __future__ import annotations

import io
import json
import logging
import os
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from dotenv import load_dotenv
from flask import (Flask, abort, jsonify, redirect, render_template,
                   request, send_file, session, url_for)

from exam_logic import (ALL_HELP_TYPES, DEFAULT_HELP_COSTS, INITIAL_SCORE,
                        deduct_help, evaluate_answer, pick_next_question_id,
                        remove_two_wrong_indices)
from models import (Exam, ExamAttempt, ExamQuestion, Student, Teacher, db)
from question_processor import call_groq, extract_text, parse_groq_response

load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-8s %(name)s – %(message)s")
logger = logging.getLogger(__name__)


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "oralify-v2-dev-secret")
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///oralify.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

    db.init_app(app)
    with app.app_context():
        db.create_all()
        _seed_demo_teacher(app)

    _register_routes(app)
    return app


def _seed_demo_teacher(app: Flask) -> None:
    """Create a default teacher account if none exists."""
    with app.app_context():
        if Teacher.query.count() == 0:
            t = Teacher(username="admin", email="admin@oralify.edu",
                        full_name="Demo Teacher")
            t.set_password("admin123")
            db.session.add(t)
            db.session.commit()
            logger.info("Demo teacher created: admin / admin123")


# ── Response helpers ──────────────────────────────────────────────────────────

def ok(payload: dict | None = None) -> tuple:
    r = {"ok": True}
    if payload:
        r.update(payload)
    return jsonify(r), 200


def err(msg: str, status: int = 400) -> tuple:
    return jsonify({"ok": False, "error": msg}), status


# ── Auth decorators ───────────────────────────────────────────────────────────

def teacher_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("role") != "teacher":
            if request.is_json or request.path.startswith("/api/"):
                return err("Authentication required.", 401)
            return redirect(url_for("teacher_login"))
        return f(*args, **kwargs)
    return wrapper


def student_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("role") != "student":
            if request.is_json or request.path.startswith("/api/"):
                return err("Authentication required.", 401)
            return redirect(url_for("student_login"))
        return f(*args, **kwargs)
    return wrapper


def _current_teacher() -> Teacher | None:
    tid = session.get("teacher_id")
    return Teacher.query.get(tid) if tid else None


def _current_student() -> Student | None:
    sid = session.get("student_id")
    return Student.query.get(sid) if sid else None


# ── Routes ────────────────────────────────────────────────────────────────────


def _handle_submit(attempt, data):
    from exam_logic import evaluate_answer
    timed_out = bool(data.get("timed_out", False))
    selected_raw = data.get("selected_options") or []
    if not selected_raw and data.get("selected_option"):
        selected_raw = [data["selected_option"]]
    selected = [str(s).strip() for s in selected_raw]
    if attempt.status == "completed":
        return err("Exam already completed.")
    qids = attempt.assigned_question_ids
    idx = attempt.current_question_index
    if idx >= len(qids):
        return err("No more questions.")
    q = ExamQuestion.query.get(qids[idx])
    exam = attempt.exam
    q_marks = exam.marks_for_question(q.id)
    current_score = attempt.final_score if attempt.final_score is not None else exam.total_marks
    if timed_out:
        new_score = max(0.0, round(current_score - q_marks * 0.3, 2))
        result_label = "timed_out"
    else:
        _, result_label = evaluate_answer(selected, q.correct_answers, current_score,
                                          timed_out=False, help_costs=exam.help_costs or {})
        if result_label == "wrong":
            new_score = 0.0
        elif result_label == "partial":
            new_score = max(0.0, current_score - q_marks)
        elif result_label == "all_wrong":
            new_score = max(0.0, current_score - q_marks * 2)
        else:
            new_score = current_score
    attempt.final_score = new_score
    results = attempt.results
    results.append({"question_id": q.id, "question_text": q.text, "options": q.options,
                    "selected": selected, "correct": q.correct_answers, "result": result_label,
                    "score_after": new_score, "timed_out": timed_out, "question_marks": q_marks})
    attempt.results = results
    attempt.current_question_index = idx + 1
    finished = (idx + 1) >= len(qids)
    if finished:
        attempt.status = "completed"
        attempt.completed_at = datetime.now(timezone.utc)
    db.session.commit()
    payload = {"result": result_label, "correct_answers": q.correct_answers, "selected": selected,
               "score": new_score, "total_marks": exam.total_marks, "finished": finished, "timed_out": timed_out}
    if not finished:
        nq = ExamQuestion.query.get(qids[idx+1])
        payload["next_question"] = nq.to_dict()
        payload["question_index"] = idx+1
        payload["timer_seconds"] = nq.timer_seconds or 60
    return ok(payload)


def _handle_use_help(attempt, data):
    help_type = (data.get("help_type") or "").strip()
    if help_type not in attempt.exam.allowed_helps:
        return err(f"Help type not allowed.")
    qids = attempt.assigned_question_ids
    idx = attempt.current_question_index
    q = ExamQuestion.query.get(qids[idx]) if idx < len(qids) else None
    result = {}
    if help_type == "hint":
        result["hint"] = q.hint if q else ""
    elif help_type == "ask_ai":
        if q:
            try:
                from question_processor import _get_groq_client, GROQ_MODEL
                client = _get_groq_client()
                opts_text = "\n".join(
                    f"{chr(65+i)}. {o}" for i, o in enumerate(q.options))
                prompt = (
                    f"Question: {q.text}\n\nOptions:\n{opts_text}\n\n"
                    "Provide:\nHINT: <one sentence, no direct answer>\nANSWER: <letter only>"
                )
                resp = client.chat.completions.create(model=GROQ_MODEL,
                                                      messages=[{"role": "user", "content": prompt}], temperature=0.2, max_tokens=200)
                raw = resp.choices[0].message.content.strip()
                hint_line = next((l.replace("HINT:", "").strip() for l in raw.splitlines(
                ) if l.startswith("HINT:")), "Think carefully.")
                answer_line = next((l.replace("ANSWER:", "").strip(
                ) for l in raw.splitlines() if l.startswith("ANSWER:")), "")
                if answer_line and len(answer_line) == 1:
                    ai_idx = ord(answer_line.upper())-65
                    if 0 <= ai_idx < len(q.options):
                        answer_line = f"{answer_line.upper()}. {
                            q.options[ai_idx]}"
                result["ai_hint"] = hint_line
                result["ai_answer"] = answer_line
            except Exception as e:
                logger.warning("AI hint: %s", e)
                result["ai_hint"] = "Consider each option carefully."
                result["ai_answer"] = ""
    elif help_type == "remove_wrong":
        if q:
            result["remove_indices"] = remove_two_wrong_indices(
                q.options, q.correct_answers)
    elif help_type == "change_question":
        all_ids = [qq.id for qq in attempt.exam.questions]
        asked = attempt.assigned_question_ids[:]
        history = session.get(f"exam_{attempt.exam_id}_history", [])
        new_ids = pick_next_question_id(all_ids, asked, history, count=1)
        if not new_ids:
            return err("No more questions.")
        assigned = attempt.assigned_question_ids
        if idx < len(assigned):
            assigned[idx] = new_ids[0]
            attempt.assigned_question_ids = assigned
        nq = ExamQuestion.query.get(new_ids[0])
        result["question"] = nq.to_dict()
        result["question_index"] = idx
        result["timer_seconds"] = nq.timer_seconds or 60
    elif help_type == "add_time":
        result["extra_seconds"] = 60
    new_score = deduct_help(attempt.final_score or attempt.exam.total_marks,
                            help_type, attempt.exam.help_costs or {})
    attempt.final_score = new_score
    helps = json.loads(attempt.helps_used)
    if help_type not in helps:
        helps.append(help_type)
    attempt.helps_used = json.dumps(helps)
    db.session.commit()
    result["score"] = new_score
    return ok(result)


def _register_routes(app: Flask) -> None:

    # ── Public pages ──────────────────────────────────────────────────────────

    @app.route("/")
    def home():
        return render_template("home.html")

    @app.route("/teacher/register", methods=["GET", "POST"])
    def teacher_register():
        if session.get("role") == "teacher":
            return redirect(url_for("teacher_dashboard"))
        if request.method == "POST":
            data = request.get_json(silent=True) or request.form
            username = (data.get("username") or "").strip()
            full_name = (data.get("full_name") or "").strip()
            email = (data.get("email") or "").strip()
            password = data.get("password") or ""
            if not all([username, full_name, email, password]):
                msg = "All fields are required."
                if request.is_json:
                    return err(msg)
                return render_template("teacher_login.html", error=msg)
            if len(password) < 6:
                msg = "Password must be at least 6 characters."
                if request.is_json:
                    return err(msg)
                return render_template("teacher_login.html", error=msg)
            if Teacher.query.filter_by(username=username).first():
                msg = "Username already taken."
                if request.is_json:
                    return err(msg)
                return render_template("teacher_login.html", error=msg)
            if Teacher.query.filter_by(email=email).first():
                msg = "Email already registered."
                if request.is_json:
                    return err(msg)
                return render_template("teacher_login.html", error=msg)
            t = Teacher(username=username, full_name=full_name, email=email)
            t.set_password(password)
            db.session.add(t)
            db.session.commit()
            session.clear()
            session["role"] = "teacher"
            session["teacher_id"] = t.id
            session["teacher_name"] = t.full_name or t.username
            if request.is_json:
                return ok({"redirect": url_for("teacher_dashboard")})
            return redirect(url_for("teacher_dashboard"))
        return render_template("teacher_login.html")

    @app.route("/teacher/login", methods=["GET", "POST"])
    def teacher_login():
        if session.get("role") == "teacher":
            return redirect(url_for("teacher_dashboard"))
        if request.method == "POST":
            data = request.get_json(silent=True) or request.form
            username = (data.get("username") or "").strip()
            password = data.get("password") or ""
            teacher = Teacher.query.filter_by(username=username).first()
            if teacher and teacher.check_password(password):
                session.clear()
                session["role"] = "teacher"
                session["teacher_id"] = teacher.id
                session["teacher_name"] = teacher.full_name or teacher.username
                if request.is_json:
                    return ok({"redirect": url_for("teacher_dashboard")})
                return redirect(url_for("teacher_dashboard"))
            if request.is_json:
                return err("Invalid username or password.")
            return render_template("teacher_login.html", error="Invalid username or password.")
        return render_template("teacher_login.html")

    @app.route("/student/login", methods=["GET", "POST"])
    def student_login():
        if session.get("role") == "student":
            return redirect(url_for("student_dashboard"))
        if request.method == "POST":
            data = request.get_json(silent=True) or request.form
            student_id = (data.get("student_id") or "").strip()
            password = data.get("password") or ""
            student = Student.query.filter_by(student_id=student_id).first()
            if student and student.check_password(password):
                next_exam = request.args.get("next_exam") or (
                    data.get("next_exam") if hasattr(data, 'get') else None)
                session.clear()
                session["role"] = "student"
                session["student_id"] = student.id
                session["student_name"] = student.full_name
                if next_exam:
                    dest = url_for("student_exam", exam_id=int(next_exam))
                    if request.is_json:
                        return ok({"redirect": dest})
                    return redirect(dest)
                if request.is_json:
                    return ok({"redirect": url_for("student_dashboard")})
                return redirect(url_for("student_dashboard"))
            if request.is_json:
                return err("Invalid student ID or password.")
            return render_template("student_login.html", error="Invalid student ID or password.")
        return render_template("student_login.html")

    @app.route("/student/register", methods=["GET", "POST"])
    def student_register():
        if request.method == "POST":
            data = request.get_json(silent=True) or request.form
            student_id = (data.get("student_id") or "").strip()
            full_name = (data.get("full_name") or "").strip()
            email = (data.get("email") or "").strip()
            password = data.get("password") or ""

            if not all([student_id, full_name, email, password]):
                if request.is_json:
                    return err("All fields are required.")
                return render_template("student_register.html", error="All fields are required.")
            if Student.query.filter_by(student_id=student_id).first():
                if request.is_json:
                    return err("Student ID already registered.")
                return render_template("student_register.html", error="Student ID already registered.")
            if Student.query.filter_by(email=email).first():
                if request.is_json:
                    return err("Email already registered.")
                return render_template("student_register.html", error="Email already registered.")

            s = Student(student_id=student_id,
                        full_name=full_name, email=email)
            s.set_password(password)
            db.session.add(s)
            db.session.commit()

            session.clear()
            session["role"] = "student"
            session["student_id"] = s.id
            session["student_name"] = s.full_name
            if request.is_json:
                return ok({"redirect": url_for("student_dashboard")})
            return redirect(url_for("student_dashboard"))
        return render_template("student_register.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("home"))

    # ── Teacher dashboard ─────────────────────────────────────────────────────

    @app.route("/teacher/dashboard")
    @teacher_required
    def teacher_dashboard():
        teacher = _current_teacher()
        return render_template("teacher_dashboard.html", teacher=teacher)

    @app.route("/teacher/exam/<int:exam_id>")
    @teacher_required
    def teacher_exam_detail(exam_id: int):
        teacher = _current_teacher()
        exam = Exam.query.filter_by(
            id=exam_id, teacher_id=teacher.id).first_or_404()
        return render_template("teacher_exam.html", teacher=teacher, exam=exam)

    # ── Teacher profile ───────────────────────────────────────────────────────

    @app.route("/api/teacher/profile", methods=["GET"])
    @teacher_required
    def api_teacher_profile():
        return ok({"teacher": _current_teacher().to_dict()})

    @app.route("/api/teacher/change_password", methods=["POST"])
    @teacher_required
    def api_teacher_change_password():
        data = request.get_json(silent=True) or {}
        teacher = _current_teacher()
        if not teacher.check_password(data.get("current_password", "")):
            return err("Current password is incorrect.")
        new_pw = (data.get("new_password") or "").strip()
        if len(new_pw) < 6:
            return err("New password must be at least 6 characters.")
        teacher.set_password(new_pw)
        db.session.commit()
        return ok({"message": "Password updated."})

    # ── Exam CRUD ─────────────────────────────────────────────────────────────

    @app.route("/api/teacher/exams", methods=["GET"])
    @teacher_required
    def api_get_exams():
        teacher = _current_teacher()
        exams = Exam.query.filter_by(teacher_id=teacher.id).order_by(
            Exam.created_at.desc()).all()
        return ok({"exams": [e.to_dict() for e in exams]})

    @app.route("/api/teacher/exams", methods=["POST"])
    @teacher_required
    def api_create_exam():
        teacher = _current_teacher()
        data = request.get_json(silent=True) or {}

        name = (data.get("name") or "").strip()
        if not name:
            return err("Exam name is required.")

        exam = Exam(
            teacher_id=teacher.id,
            name=name,
            course_name=data.get("course_name", "").strip(),
            course_code=data.get("course_code", "").strip(),
            description=data.get("description", "").strip(),
            questions_per_student=int(data.get("questions_per_student", 1)),
            is_public=bool(data.get("is_public", False)),
            total_marks=float(data.get("total_marks", 10.0)),
        )

        if data.get("start_time"):
            # Store as naive datetime — the frontend sends local time from datetime-local input
            raw = data["start_time"].replace("Z", "")
            exam.start_time = datetime.fromisoformat(
                raw.split("+")[0].split(".")[0])
        if data.get("end_time"):
            raw = data["end_time"].replace("Z", "")
            exam.end_time = datetime.fromisoformat(
                raw.split("+")[0].split(".")[0])

        if data.get("allowed_helps") is not None:
            exam.allowed_helps = data["allowed_helps"]
        if data.get("help_costs"):
            exam.help_costs = data["help_costs"]

        db.session.add(exam)
        db.session.commit()
        return ok({"exam": exam.to_dict()})

    @app.route("/api/teacher/exams/<int:exam_id>", methods=["GET"])
    @teacher_required
    def api_get_exam(exam_id: int):
        teacher = _current_teacher()
        exam = Exam.query.filter_by(
            id=exam_id, teacher_id=teacher.id).first_or_404()
        return ok({"exam": exam.to_dict(include_questions=True)})

    @app.route("/api/teacher/exams/<int:exam_id>", methods=["PUT"])
    @teacher_required
    def api_update_exam(exam_id: int):
        teacher = _current_teacher()
        exam = Exam.query.filter_by(
            id=exam_id, teacher_id=teacher.id).first_or_404()
        data = request.get_json(silent=True) or {}

        for field in ("name", "course_name", "course_code", "description"):
            if field in data:
                setattr(exam, field, data[field].strip())
        if "questions_per_student" in data:
            exam.questions_per_student = int(data["questions_per_student"])
        if "is_active" in data:
            exam.is_active = bool(data["is_active"])
        if "is_public" in data:
            exam.is_public = bool(data["is_public"])
        if "total_marks" in data:
            exam.total_marks = float(data["total_marks"])
        if "start_time" in data:
            if data["start_time"]:
                raw = data["start_time"].replace("Z", "")
                exam.start_time = datetime.fromisoformat(
                    raw.split("+")[0].split(".")[0])
            else:
                exam.start_time = None
        if "end_time" in data:
            if data["end_time"]:
                raw = data["end_time"].replace("Z", "")
                exam.end_time = datetime.fromisoformat(
                    raw.split("+")[0].split(".")[0])
            else:
                exam.end_time = None
        if "allowed_helps" in data:
            exam.allowed_helps = data["allowed_helps"]
        if "help_costs" in data:
            exam.help_costs = data["help_costs"]

        db.session.commit()
        return ok({"exam": exam.to_dict()})

    @app.route("/api/teacher/exams/<int:exam_id>", methods=["DELETE"])
    @teacher_required
    def api_delete_exam(exam_id: int):
        teacher = _current_teacher()
        exam = Exam.query.filter_by(
            id=exam_id, teacher_id=teacher.id).first_or_404()
        db.session.delete(exam)
        db.session.commit()
        return ok({"deleted": exam_id})

    # ── Question management ───────────────────────────────────────────────────

    @app.route("/api/teacher/exams/<int:exam_id>/questions", methods=["GET"])
    @teacher_required
    def api_get_questions(exam_id: int):
        teacher = _current_teacher()
        exam = Exam.query.filter_by(
            id=exam_id, teacher_id=teacher.id).first_or_404()
        return ok({"questions": [q.to_dict(reveal=True) for q in exam.questions]})

    @app.route("/api/teacher/exams/<int:exam_id>/export_questions", methods=["GET"])
    @teacher_required
    def api_export_questions(exam_id: int):
        """Export questions as Excel."""
        teacher = _current_teacher()
        exam = Exam.query.filter_by(
            id=exam_id, teacher_id=teacher.id).first_or_404()
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        except ImportError:
            return json_error("openpyxl not installed.", 500)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Questions"

        hf = Font(bold=True, color="FFFFFF", size=11)
        hfil = PatternFill("solid", fgColor="2563EB")
        ctr = Alignment(horizontal="center", vertical="center", wrap_text=True)
        lft = Alignment(horizontal="left",   vertical="center", wrap_text=True)
        thin = Side(style="thin", color="D1D5DB")
        bdr = Border(left=thin, right=thin, top=thin, bottom=thin)

        headers = ["#", "text", "options",
                   "correct_answers", "hint", "timer_seconds"]
        widths = [4, 45, 40, 25, 25, 12]
        for col, (h, w) in enumerate(zip(headers, widths), 1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.font = hf
            cell.fill = hfil
            cell.alignment = ctr
            cell.border = bdr
            ws.column_dimensions[cell.column_letter].width = w
        ws.row_dimensions[1].height = 18

        for row, q in enumerate(exam.questions, 2):
            vals = [
                row - 1,
                q.text,
                "|".join(q.options),
                "|".join(q.correct_answers),
                q.hint or "",
                q.timer_seconds or "",
            ]
            aligns = [ctr, lft, lft, lft, lft, ctr]
            for col, (v, a) in enumerate(zip(vals, aligns), 1):
                cell = ws.cell(row=row, column=col, value=v)
                cell.alignment = a
                cell.border = bdr
            ws.row_dimensions[row].height = 30

        ws.freeze_panes = "A2"
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        fname = f"{exam.name[:30].replace(' ', '_')}_questions.xlsx"
        return send_file(buf, as_attachment=True, download_name=fname,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.route("/api/teacher/exams/<int:exam_id>/questions", methods=["POST"])
    @teacher_required
    def api_add_questions(exam_id: int):
        """Bulk-add questions (from AI or manual). Body: {questions: [...]}"""
        teacher = _current_teacher()
        exam = Exam.query.filter_by(
            id=exam_id, teacher_id=teacher.id).first_or_404()
        data = request.get_json(silent=True) or {}
        questions_data = data.get("questions", [])
        if not questions_data:
            return err("No questions provided.")

        start_pos = len(exam.questions)
        new_qs = []
        for i, q in enumerate(questions_data):
            correct = q.get("correct_answers") or (
                [q["correct_answer"]] if q.get("correct_answer") else [])
            eq = ExamQuestion(
                exam_id=exam.id,
                position=start_pos + i,
                text=q["text"].strip(),
                hint=q.get("hint", "").strip(),
                timer_seconds=q.get("timer_seconds"),
            )
            eq.options = [o.strip() for o in q["options"]]
            eq.correct_answers = [c.strip() for c in correct]
            new_qs.append(eq)

        db.session.add_all(new_qs)
        db.session.commit()
        return ok({"added": len(new_qs), "total": len(exam.questions)})

    @app.route("/api/teacher/questions/<int:q_id>", methods=["PUT"])
    @teacher_required
    def api_update_question(q_id: int):
        teacher = _current_teacher()
        q = ExamQuestion.query.get_or_404(q_id)
        if q.exam.teacher_id != teacher.id:
            abort(403)
        data = request.get_json(silent=True) or {}
        if "text" in data:
            q.text = data["text"].strip()
        if "options" in data:
            q.options = [o.strip() for o in data["options"]]
        if "correct_answers" in data:
            q.correct_answers = data["correct_answers"]
        if "hint" in data:
            q.hint = data["hint"].strip()
        if "timer_seconds" in data:
            q.timer_seconds = data["timer_seconds"]
        db.session.commit()
        return ok({"question": q.to_dict(reveal=True)})

    @app.route("/api/teacher/questions/<int:q_id>", methods=["DELETE"])
    @teacher_required
    def api_delete_question(q_id: int):
        teacher = _current_teacher()
        q = ExamQuestion.query.get_or_404(q_id)
        if q.exam.teacher_id != teacher.id:
            abort(403)
        db.session.delete(q)
        db.session.commit()
        return ok({"deleted": q_id})

    @app.route("/api/teacher/exams/<int:exam_id>/questions/clear", methods=["POST"])
    @teacher_required
    def api_clear_questions(exam_id: int):
        teacher = _current_teacher()
        exam = Exam.query.filter_by(
            id=exam_id, teacher_id=teacher.id).first_or_404()
        ExamQuestion.query.filter_by(exam_id=exam.id).delete()
        db.session.commit()
        return ok({"cleared": True})

    # ── AI import ─────────────────────────────────────────────────────────────

    @app.route("/api/teacher/ai/upload", methods=["POST"])
    @teacher_required
    def api_ai_upload():
        if "file" not in request.files:
            return err("No file provided.")
        file = request.files["file"]
        instructions = (request.form.get("instructions") or "").strip()
        try:
            raw = extract_text(file)
            questions = parse_groq_response(
                call_groq(raw, instructions=instructions))
        except Exception as e:
            logger.exception("AI upload error")
            return err(str(e))
        return ok({"questions": questions})

    @app.route("/api/teacher/ai/text", methods=["POST"])
    @teacher_required
    def api_ai_text():
        data = request.get_json(silent=True) or {}
        raw = (data.get("text") or "").strip()
        if not raw:
            return err("No text provided.")
        instructions = (data.get("instructions") or "").strip()
        try:
            questions = parse_groq_response(
                call_groq(raw, instructions=instructions))
        except Exception as e:
            logger.exception("AI text error")
            return err(str(e))
        return ok({"questions": questions})

    # ── Exam results (teacher view) ───────────────────────────────────────────

    @app.route("/api/teacher/exams/<int:exam_id>/results", methods=["GET"])
    @teacher_required
    def api_exam_results(exam_id: int):
        teacher = _current_teacher()
        exam = Exam.query.filter_by(
            id=exam_id, teacher_id=teacher.id).first_or_404()
        attempts = (ExamAttempt.query.filter_by(exam_id=exam.id)
                    .order_by(ExamAttempt.completed_at.desc()).all())
        return ok({"results": [a.to_dict() for a in attempts]})

    @app.route("/api/teacher/attempts/<int:attempt_id>/override", methods=["POST"])
    @teacher_required
    def api_override_score(attempt_id: int):
        teacher = _current_teacher()
        attempt = ExamAttempt.query.get_or_404(attempt_id)
        if attempt.exam.teacher_id != teacher.id:
            abort(403)
        data = request.get_json(silent=True) or {}
        try:
            score = float(data["score"])
        except (KeyError, ValueError, TypeError):
            return err("Invalid score.")
        attempt.score_override = score
        db.session.commit()
        return ok({"attempt": attempt.to_dict()})

    @app.route("/api/teacher/attempts/<int:attempt_id>", methods=["DELETE"])
    @teacher_required
    def api_delete_attempt(attempt_id: int):
        teacher = _current_teacher()
        attempt = ExamAttempt.query.get_or_404(attempt_id)
        if attempt.exam.teacher_id != teacher.id:
            abort(403)
        db.session.delete(attempt)
        db.session.commit()
        return ok({"deleted": attempt_id})

    @app.route("/api/teacher/exams/<int:exam_id>/export", methods=["GET"])
    @teacher_required
    def api_export_results(exam_id: int):
        teacher = _current_teacher()
        exam = Exam.query.filter_by(
            id=exam_id, teacher_id=teacher.id).first_or_404()
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        except ImportError:
            return err("openpyxl not installed.", 500)

        attempts = (ExamAttempt.query.filter_by(exam_id=exam.id, status="completed")
                    .order_by(ExamAttempt.completed_at).all())

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = exam.name[:31]

        hf = Font(bold=True, color="FFFFFF", size=11)
        hfill = PatternFill("solid", fgColor="2563EB")
        center = Alignment(horizontal="center", vertical="center")
        left = Alignment(horizontal="left",   vertical="center")
        thin = Side(style="thin", color="D1D5DB")
        bdr = Border(left=thin, right=thin, top=thin, bottom=thin)

        def sfill(s):
            if s is None:
                return PatternFill()
            if s >= 8:
                return PatternFill("solid", fgColor="DCFCE7")
            if s >= 5:
                return PatternFill("solid", fgColor="FEF9C3")
            return PatternFill("solid", fgColor="FEE2E2")

        headers = ["#", "Student ID", "Student Name",
                   "Score / 10", "Time Started", "Time Completed"]
        widths = [5, 15, 28, 14, 20, 20]
        for col, (h, w) in enumerate(zip(headers, widths), 1):
            c = ws.cell(row=1, column=col, value=h)
            c.font = hf
            c.fill = hfill
            c.alignment = center
            c.border = bdr
            ws.column_dimensions[c.column_letter].width = w
        ws.row_dimensions[1].height = 22

        for row, att in enumerate(attempts, 2):
            vals = [row-1, att.student.student_id if att.student else "",
                    att.student.full_name if att.student else "",
                    att.display_score,
                    att.started_at.strftime(
                        "%Y-%m-%d %H:%M") if att.started_at else "",
                    att.completed_at.strftime("%Y-%m-%d %H:%M") if att.completed_at else ""]
            aligns = [center, center, left, center, center, center]
            for col, (v, a) in enumerate(zip(vals, aligns), 1):
                c = ws.cell(row=row, column=col, value=v)
                c.alignment = a
                c.border = bdr
                if col == 4:
                    c.fill = sfill(att.display_score)
                    c.font = Font(bold=True)

        if attempts:
            sr = len(attempts) + 3
            ws.cell(row=sr,   column=1, value="Total").font = Font(bold=True)
            ws.cell(row=sr,   column=2, value=len(attempts))
            ws.cell(row=sr+1, column=1, value="Average").font = Font(bold=True)
            scores = [
                a.display_score for a in attempts if a.display_score is not None]
            ws.cell(row=sr+1, column=2, value=round(sum(scores) /
                    len(scores), 2) if scores else 0)

        ws.freeze_panes = "A2"
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        fname = f"{exam.name[:30].replace(' ', '_')}_results.xlsx"
        return send_file(buf, as_attachment=True, download_name=fname,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # ── Exam share link & QR ─────────────────────────────────────────────────

    @app.route("/api/teacher/exams/<int:exam_id>/share", methods=["GET"])
    @teacher_required
    def api_exam_share(exam_id: int):
        teacher = _current_teacher()
        exam = Exam.query.filter_by(
            id=exam_id, teacher_id=teacher.id).first_or_404()
        from flask import request as req
        base = req.host_url.rstrip("/")
        link = f"{base}/exam/{exam_id}"
        # Generate QR as base64 PNG
        try:
            import qrcode
            import io
            import base64
            qr = qrcode.QRCode(box_size=6, border=2)
            qr.add_data(link)
            qr.make(fit=True)
            img = qr.make_image(fill_color="#2563eb", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            qr_b64 = base64.b64encode(buf.getvalue()).decode()
            qr_data = f"data:image/png;base64,{qr_b64}"
        except ImportError:
            qr_data = None
        return ok({"link": link, "qr": qr_data, "exam_name": exam.name})

    # ── Student dashboard ─────────────────────────────────────────────────────

    @app.route("/student/dashboard")
    @student_required
    def student_dashboard():
        student = _current_student()
        return render_template("student_dashboard.html", student=student)

    @app.route("/student/exam/<int:exam_id>")
    @student_required
    def student_exam(exam_id: int):
        student = _current_student()
        exam = Exam.query.get_or_404(exam_id)
        if not exam.is_open:
            return redirect(url_for("student_dashboard"))
        # Check if student already completed this exam
        existing = ExamAttempt.query.filter_by(
            exam_id=exam_id, student_id=student.id, status="completed").first()
        if existing:
            return redirect(url_for("student_dashboard"))
        return render_template("student_exam.html", student=student, exam=exam)

    # ── Student API ───────────────────────────────────────────────────────────

    # ── Public / guest exam ──────────────────────────────────────────────────

    @app.route("/public")
    def public_exams():
        exams = [e for e in Exam.query.filter_by(
            is_active=True, is_public=True).all() if e.is_open]
        return render_template("public_exam_list.html", exams=exams)

    @app.route("/exam/<int:exam_id>")
    def exam_direct_link(exam_id: int):
        exam = Exam.query.get_or_404(exam_id)
        if session.get("role") == "student":
            return redirect(url_for("student_exam", exam_id=exam_id))
        if exam.is_public and exam.is_open:
            return render_template("public_exam.html", exam=exam)
        session["next_exam"] = exam_id
        return redirect(url_for("student_login") + f"?next_exam={exam_id}")

    @app.route("/api/public/start_exam", methods=["POST"])
    def api_public_start_exam():
        data = request.get_json(silent=True) or {}
        guest_name = (data.get("guest_name") or "").strip()
        if not guest_name:
            return err("Please enter your name.")
        exam = Exam.query.get_or_404(data.get("exam_id"))
        if not exam.is_public:
            return err("This exam requires an account.")
        if not exam.is_open:
            return err("This exam is not currently available.")
        if not exam.questions:
            return err("This exam has no questions.")
        all_ids = [q.id for q in exam.questions]
        asked_ids = session.get(f"pub_{exam.id}_asked", [])
        history = session.get(f"pub_{exam.id}_history", [])
        chosen = pick_next_question_id(all_ids, asked_ids, history, count=min(
            exam.questions_per_student, len(all_ids)))
        asked_ids.extend(chosen)
        history.extend(chosen)
        session[f"pub_{exam.id}_asked"] = asked_ids[-len(all_ids)*2:]
        session[f"pub_{exam.id}_history"] = history[-20:]
        attempt = ExamAttempt(exam_id=exam.id, student_id=None,
                              guest_name=guest_name, final_score=exam.total_marks)
        attempt.assigned_question_ids = chosen
        attempt.results = []
        db.session.add(attempt)
        db.session.commit()
        q = ExamQuestion.query.get(chosen[0])
        return ok({"attempt_id": attempt.id, "guest_name": guest_name, "question": q.to_dict(),
                   "question_index": 0, "total_questions": len(chosen), "score": attempt.final_score,
                   "timer_seconds": q.timer_seconds or 60, "allowed_helps": exam.allowed_helps,
                   "help_costs": {**DEFAULT_HELP_COSTS, **exam.help_costs}, "total_marks": exam.total_marks})

    @app.route("/api/public/submit_answer", methods=["POST"])
    def api_public_submit_answer():
        data = request.get_json(silent=True) or {}
        attempt = ExamAttempt.query.get_or_404(data.get("attempt_id"))
        if attempt.student_id is not None:
            abort(403)
        return _handle_submit(attempt, data)

    @app.route("/api/public/use_help", methods=["POST"])
    def api_public_use_help():
        data = request.get_json(silent=True) or {}
        attempt = ExamAttempt.query.get_or_404(data.get("attempt_id"))
        if attempt.student_id is not None:
            abort(403)
        return _handle_use_help(attempt, data)

    @app.route("/api/public/finish_early", methods=["POST"])
    def api_public_finish_early():
        data = request.get_json(silent=True) or {}
        attempt = ExamAttempt.query.get_or_404(data.get("attempt_id"))
        if attempt.student_id is not None:
            abort(403)
        attempt.status = "completed"
        attempt.completed_at = datetime.now(timezone.utc)
        db.session.commit()
        return ok({"score": attempt.final_score, "helps_used": json.loads(attempt.helps_used), "total_marks": attempt.exam.total_marks})

    # ── Teacher: scoring ──────────────────────────────────────────────────────

    @app.route("/api/teacher/exams/<int:exam_id>/scoring", methods=["POST"])
    @teacher_required
    def api_update_scoring(exam_id: int):
        teacher = _current_teacher()
        exam = Exam.query.filter_by(
            id=exam_id, teacher_id=teacher.id).first_or_404()
        data = request.get_json(silent=True) or {}
        if "total_marks" in data:
            exam.total_marks = float(data["total_marks"])
        if "question_marks" in data:
            exam.question_marks = data["question_marks"]
        if "is_public" in data:
            exam.is_public = bool(data["is_public"])
        db.session.commit()
        return ok({"exam": exam.to_dict()})

    # ── Student: review ───────────────────────────────────────────────────────

    @app.route("/api/student/attempt/<int:attempt_id>/review", methods=["GET"])
    @student_required
    def api_attempt_review(attempt_id: int):
        student = _current_student()
        attempt = ExamAttempt.query.get_or_404(attempt_id)
        if attempt.student_id != student.id:
            abort(403)
        if attempt.status != "completed":
            return err("Exam not completed yet.")
        return ok({"attempt": attempt.to_dict(include_results=True), "total_marks": attempt.exam.total_marks})

    @app.route("/api/student/available_exams", methods=["GET"])
    @student_required
    def api_available_exams():
        student = _current_student()
        all_exams = Exam.query.filter_by(is_active=True).all()
        result = []
        for exam in all_exams:
            completed = ExamAttempt.query.filter_by(
                exam_id=exam.id, student_id=student.id, status="completed").first()
            in_progress = ExamAttempt.query.filter_by(
                exam_id=exam.id, student_id=student.id, status="in_progress").first()
            result.append({
                **exam.to_dict(),
                "student_status": "completed" if completed else ("in_progress" if in_progress else "not_started"),
            })
        return ok({"exams": result})

    @app.route("/api/student/start_exam", methods=["POST"])
    @student_required
    def api_start_exam():
        student = _current_student()
        data = request.get_json(silent=True) or {}
        exam_id = data.get("exam_id")
        exam = Exam.query.get_or_404(exam_id)

        if not exam.is_open:
            return err("This exam is not currently available.")
        if not exam.questions:
            return err("This exam has no questions yet.")

        # Resume in-progress attempt
        attempt = ExamAttempt.query.filter_by(
            exam_id=exam.id, student_id=student.id, status="in_progress").first()

        if not attempt:
            # Check already completed
            if ExamAttempt.query.filter_by(
                    exam_id=exam.id, student_id=student.id, status="completed").first():
                return err("You have already completed this exam.")

            # Assign questions using cooldown-aware pool
            all_ids = [q.id for q in exam.questions]
            # Global asked_ids per exam stored in a simple table-free way via session
            asked_key = f"exam_{exam.id}_asked"
            history_key = f"exam_{exam.id}_history"
            asked_ids = session.get(asked_key, [])
            history = session.get(history_key, [])

            chosen = pick_next_question_id(
                all_ids, asked_ids, history,
                count=min(exam.questions_per_student, len(all_ids))
            )
            # Update session pool
            asked_ids.extend(chosen)
            history.extend(chosen)
            session[asked_key] = asked_ids[-len(all_ids)*2:]
            session[history_key] = history[-20:]

            attempt = ExamAttempt(
                exam_id=exam.id,
                student_id=student.id,
                final_score=exam.total_marks,
            )
            attempt.assigned_question_ids = chosen
            attempt.results = []
            db.session.add(attempt)
            db.session.commit()

        # Return current question
        qids = attempt.assigned_question_ids
        idx = attempt.current_question_index
        if idx >= len(qids):
            return err("All questions answered.")

        q = ExamQuestion.query.get(qids[idx])
        return ok({
            "attempt_id":       attempt.id,
            "question":         q.to_dict(),
            "question_index":   idx,
            "total_questions":  len(qids),
            "score":            attempt.final_score,
            "timer_seconds":    q.timer_seconds or 60,
            "allowed_helps":    exam.allowed_helps,
            "help_costs":       {**DEFAULT_HELP_COSTS, **exam.help_costs},
        })

    @app.route("/api/student/submit_answer", methods=["POST"])
    @student_required
    def api_submit_answer():
        student = _current_student()
        data = request.get_json(silent=True) or {}
        attempt = ExamAttempt.query.get_or_404(data.get("attempt_id"))
        if attempt.student_id != student.id:
            abort(403)
        return _handle_submit(attempt, data)

    @app.route("/api/student/use_help", methods=["POST"])
    @student_required
    def api_use_help():
        student = _current_student()
        data = request.get_json(silent=True) or {}
        attempt = ExamAttempt.query.get_or_404(data.get("attempt_id"))
        if attempt.student_id != student.id:
            abort(403)
        return _handle_use_help(attempt, data)

    @app.route("/api/student/finish_early", methods=["POST"])
    @student_required
    def api_finish_early():
        student = _current_student()
        data = request.get_json(silent=True) or {}
        attempt = ExamAttempt.query.get_or_404(data.get("attempt_id"))
        if attempt.student_id != student.id:
            abort(403)
        attempt.status = "completed"
        attempt.completed_at = datetime.now(timezone.utc)
        db.session.commit()
        return ok({"score": attempt.final_score, "helps_used": json.loads(attempt.helps_used)})

    @app.route("/api/student/my_results", methods=["GET"])
    @student_required
    def api_my_results():
        student = _current_student()
        attempts = (ExamAttempt.query.filter_by(student_id=student.id)
                    .order_by(ExamAttempt.started_at.desc()).all())
        return ok({"results": [a.to_dict() for a in attempts]})

    # ── Teacher account settings & stats ─────────────────────────────────────

    @app.route("/api/teacher/stats", methods=["GET"])
    @teacher_required
    def api_teacher_stats():
        teacher = _current_teacher()
        exams = Exam.query.filter_by(teacher_id=teacher.id).all()
        all_attempts = ExamAttempt.query.join(Exam).filter(
            Exam.teacher_id == teacher.id, ExamAttempt.status == "completed").all()
        scores = [
            a.display_score for a in all_attempts if a.display_score is not None]
        return ok({
            "total_exams":      len(exams),
            "active_exams":     sum(1 for e in exams if e.status == "open"),
            "total_questions":  sum(len(e.questions) for e in exams),
            "total_attempts":   len(all_attempts),
            "unique_students":  len(set(a.student_id for a in all_attempts)),
            "avg_score":        round(sum(scores)/len(scores), 2) if scores else None,
        })

    @app.route("/api/teacher/update_profile", methods=["POST"])
    @teacher_required
    def api_teacher_update_profile():
        teacher = _current_teacher()
        data = request.get_json(silent=True) or {}
        if "full_name" in data:
            teacher.full_name = data["full_name"].strip()
        if "email" in data:
            email = data["email"].strip()
            ex = Teacher.query.filter_by(email=email).first()
            if ex and ex.id != teacher.id:
                return err("Email already in use.")
            teacher.email = email
        db.session.commit()
        session["teacher_name"] = teacher.full_name or teacher.username
        return ok({"teacher": teacher.to_dict()})

    @app.route("/api/teacher/delete_account", methods=["POST"])
    @teacher_required
    def api_teacher_delete_account():
        teacher = _current_teacher()
        data = request.get_json(silent=True) or {}
        if not teacher.check_password(data.get("password", "")):
            return err("Incorrect password.")
        db.session.delete(teacher)
        db.session.commit()
        session.clear()
        return ok({"redirect": "/"})

    # ── Student account settings & stats ─────────────────────────────────────

    @app.route("/api/student/stats", methods=["GET"])
    @student_required
    def api_student_stats():
        student = _current_student()
        attempts = ExamAttempt.query.filter_by(student_id=student.id).all()
        completed = [a for a in attempts if a.status == "completed"]
        scores = [a.display_score for a in completed if a.display_score is not None]
        return ok({
            "total_attempts": len(attempts),
            "completed":      len(completed),
            "avg_score":      round(sum(scores)/len(scores), 2) if scores else None,
            "best_score":     max(scores) if scores else None,
        })

    @app.route("/api/student/update_profile", methods=["POST"])
    @student_required
    def api_student_update_profile():
        student = _current_student()
        data = request.get_json(silent=True) or {}
        if "full_name" in data:
            student.full_name = data["full_name"].strip()
        if "email" in data:
            email = data["email"].strip()
            ex = Student.query.filter_by(email=email).first()
            if ex and ex.id != student.id:
                return err("Email already in use.")
            student.email = email
        db.session.commit()
        session["student_name"] = student.full_name
        return ok({"student": student.to_dict()})

    @app.route("/api/student/change_password", methods=["POST"])
    @student_required
    def api_student_change_password():
        student = _current_student()
        data = request.get_json(silent=True) or {}
        if not student.check_password(data.get("current_password", "")):
            return err("Current password is incorrect.")
        new_pw = (data.get("new_password") or "").strip()
        if len(new_pw) < 6:
            return err("New password must be at least 6 characters.")
        student.set_password(new_pw)
        db.session.commit()
        return ok({"message": "Password updated."})

    @app.route("/api/student/delete_account", methods=["POST"])
    @student_required
    def api_student_delete_account():
        student = _current_student()
        data = request.get_json(silent=True) or {}
        if not student.check_password(data.get("password", "")):
            return err("Incorrect password.")
        db.session.delete(student)
        db.session.commit()
        session.clear()
        return ok({"redirect": "/"})


# ── Entry point ───────────────────────────────────────────────────────────────
app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
