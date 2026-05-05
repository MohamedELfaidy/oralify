"""
app.py – Oralify Flask application.
"""

from __future__ import annotations

import io
import logging
import os
from functools import wraps
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, session, send_file

from exam_logic import (
    HELP_COSTS,
    INITIAL_SCORE,
    clear_student_session,
    deduct_score,
    elapsed_seconds,
    evaluate_multi_answer,
    get_next_question_id,
    get_session_state,
    init_session,
    remove_two_wrong_indices,
    reset_question_pool,
    set_score,
)
from models import Question, StudentResult, db
from question_processor import call_groq, extract_text, parse_groq_response

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get(
        "SECRET_KEY", "oralify-dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///exam.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

    db.init_app(app)
    with app.app_context():
        db.create_all()
        _migrate_correct_answer_column(app)

    _register_routes(app)
    return app


def _migrate_correct_answer_column(app: Flask) -> None:
    """
    One-time migration: if the DB still has the old single correct_answer column,
    copy its data into the new correct_answers column.
    """
    import sqlalchemy as sa
    with app.app_context():
        insp = sa.inspect(db.engine)
        cols = [c["name"] for c in insp.get_columns("questions")]
        if "correct_answer" in cols and "correct_answers" not in cols:
            with db.engine.connect() as conn:
                conn.execute(sa.text(
                    "ALTER TABLE questions ADD COLUMN correct_answers TEXT"
                ))
                conn.execute(sa.text(
                    "UPDATE questions SET correct_answers = json_array(correct_answer)"
                ))
                conn.commit()
            logger.info("Migrated correct_answer → correct_answers column.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def json_error(message: str, status: int = 400) -> tuple:
    return jsonify({"ok": False, "error": message}), status


def json_ok(payload: dict[str, Any] | None = None) -> tuple:
    response = {"ok": True}
    if payload:
        response.update(payload)
    return jsonify(response), 200


def require_active_session(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "student_name" not in session:
            return json_error("No active exam session.", 400)
        return f(*args, **kwargs)
    return wrapper


def _question_for_student(question: Question) -> dict:
    """Send question data to student — no correct answers revealed."""
    return {
        "id": question.id,
        "text": question.text,
        "options": question.options,
        "multi_answer": len(question.correct_answers) > 1,
        "num_correct": len(question.correct_answers),
    }


def _load_next_question():
    """
    Pick next question using cooldown-aware pool.
    Auto-resets cycle when all questions exhausted.
    """
    all_ids = [row.id for row in Question.query.with_entities(
        Question.id).all()]
    if not all_ids:
        return None

    asked: list[int] = session.get("asked_ids", [])
    remaining = [q for q in all_ids if q not in asked]

    if not remaining:
        reset_question_pool(session)

    next_id = get_next_question_id(session, all_ids)
    if next_id is None:
        return None

    return Question.query.get(next_id)


# ── Routes ────────────────────────────────────────────────────────────────────

def _register_routes(app: Flask) -> None:

    @app.route("/")
    def index():
        return render_template("index.html")

    # ── Import ────────────────────────────────────────────────────────────────

    @app.route("/upload", methods=["POST"])
    def upload():
        if "file" not in request.files:
            return json_error("No file part in request.")
        file = request.files["file"]
        if not file.filename:
            return json_error("No file selected.")
        try:
            raw_text = extract_text(file)
        except ValueError as exc:
            return json_error(str(exc))
        except Exception as exc:
            logger.exception("Text extraction error")
            return json_error(f"Text extraction failed: {exc}")
        try:
            questions = parse_groq_response(call_groq(raw_text))
        except EnvironmentError as exc:
            return json_error(str(exc))
        except ValueError as exc:
            return json_error(f"AI processing failed: {exc}")
        except Exception as exc:
            logger.exception("Groq error")
            return json_error(f"AI service error: {exc}")
        return json_ok({"questions": questions})

    @app.route("/manual_process", methods=["POST"])
    def manual_process():
        data = request.get_json(silent=True) or {}
        raw_text = (data.get("text") or "").strip()
        if not raw_text:
            return json_error("No text provided.")
        try:
            questions = parse_groq_response(call_groq(raw_text))
        except Exception as exc:
            logger.exception("manual_process error")
            return json_error(f"AI processing failed: {exc}")
        return json_ok({"questions": questions})

    # ── Question CRUD ─────────────────────────────────────────────────────────

    @app.route("/save_questions", methods=["POST"])
    def save_questions():
        data = request.get_json(silent=True) or {}
        questions_data = data.get("questions", [])
        if not questions_data:
            return json_error("No questions provided.")

        for idx, q in enumerate(questions_data):
            if not q.get("text", "").strip():
                return json_error(f"Question {idx + 1} has no text.")
            opts = q.get("options", [])
            if len(opts) < 2:
                return json_error(f"Question {idx + 1} needs at least 2 options.")
            correct = q.get("correct_answers") or (
                [q["correct_answer"]] if q.get("correct_answer") else []
            )
            if not correct:
                return json_error(f"Question {idx + 1} has no correct answer.")
            opt_stripped = [o.strip() for o in opts]
            for c in correct:
                if c.strip() not in opt_stripped:
                    return json_error(
                        f"Question {
                            idx + 1}: correct answer '{c}' not in options."
                    )

        new_questions = []
        for q in questions_data:
            question = Question(
                text=q["text"].strip(),
                hint=q.get("hint", "").strip(),
            )
            question.options = [o.strip() for o in q["options"]]
            correct = q.get("correct_answers") or [q.get("correct_answer", "")]
            question.correct_answers = [c.strip()
                                        for c in correct if c.strip()]
            new_questions.append(question)

        db.session.add_all(new_questions)
        db.session.commit()
        return json_ok({"saved": len(new_questions), "total": Question.query.count()})

    @app.route("/get_questions", methods=["GET"])
    def get_questions():
        questions = Question.query.order_by(Question.id).all()
        return json_ok({"questions": [q.to_dict() for q in questions]})

    @app.route("/update_question/<int:question_id>", methods=["POST"])
    def update_question(question_id: int):
        question = Question.query.get_or_404(question_id)
        data = request.get_json(silent=True) or {}
        if "text" in data:
            question.text = data["text"].strip()
        if "options" in data:
            question.options = [o.strip() for o in data["options"]]
        if "correct_answers" in data:
            question.correct_answers = [c.strip()
                                        for c in data["correct_answers"]]
        elif "correct_answer" in data:
            question.correct_answer = data["correct_answer"].strip()
        if "hint" in data:
            question.hint = data["hint"].strip()
        db.session.commit()
        return json_ok({"question": question.to_dict()})

    @app.route("/delete_question/<int:question_id>", methods=["POST"])
    def delete_question(question_id: int):
        question = Question.query.get_or_404(question_id)
        db.session.delete(question)
        db.session.commit()
        return json_ok({"deleted_id": question_id})

    @app.route("/clear_all_questions", methods=["POST"])
    def clear_all_questions():
        Question.query.delete()
        db.session.commit()
        return json_ok({"message": "All questions deleted."})

    # ── Exam ──────────────────────────────────────────────────────────────────

    @app.route("/start_exam", methods=["POST"])
    def start_exam():
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return json_error("Student name is required.")
        if Question.query.count() == 0:
            return json_error("No questions in the database.")

        init_session(session, name)
        question = _load_next_question()
        if question is None:
            return json_error("Could not load a question.")

        return json_ok({
            "student_name": name,
            "score": INITIAL_SCORE,
            "question": _question_for_student(question),
        })

    @app.route("/submit_answer", methods=["POST"])
    @require_active_session
    def submit_answer():
        """
        Body: {selected_options: list[str], timed_out?: bool}

        Scoring:
          - Timed out                       → 7 (fixed)
          - Single-answer, correct          → keep score
          - Single-answer, wrong            → 0
          - Multi-answer, all correct       → keep score
          - Multi-answer, one right one wrong → -1
          - Multi-answer, both wrong        → -2
        """
        data = request.get_json(silent=True) or {}
        timed_out = bool(data.get("timed_out", False))

        # Accept both selected_options (list) and selected_option (string, legacy)
        selected_raw = data.get("selected_options") or []
        if not selected_raw and data.get("selected_option"):
            selected_raw = [data["selected_option"]]
        selected = [str(s).strip() for s in selected_raw if str(s).strip()]

        if not selected and not timed_out:
            return json_error("No option selected.")

        current_id = session.get("current_id")
        if current_id is None:
            return json_error("No current question in session.")

        question = Question.query.get(current_id)
        if question is None:
            return json_error("Current question not found.")

        time_taken = elapsed_seconds(session)
        current_score: float = session.get("score", INITIAL_SCORE)

        if timed_out:
            set_score(session, 7.0)
            result_label = "timed_out"
        else:
            new_score, result_label = evaluate_multi_answer(
                selected, question.correct_answers, current_score
            )
            set_score(session, new_score)

        exam_state = get_session_state(session)

        return json_ok({
            # correct|wrong|all_correct|partial|all_wrong|timed_out
            "result":          result_label,
            "correct_answers": question.correct_answers,
            "selected":        selected,
            "score":           exam_state["score"],
            "helps_used":      exam_state["helps_used"],
            "student_name":    exam_state["student_name"],
            "time_taken":      time_taken,
            "timed_out":       timed_out,
            "finished":        True,
        })

    # ── Help ──────────────────────────────────────────────────────────────────

    @app.route("/use_help", methods=["POST"])
    @require_active_session
    def use_help():
        data = request.get_json(silent=True) or {}
        help_type = (data.get("help_type") or "").strip()
        if help_type not in HELP_COSTS:
            return json_error(f"Unknown help type: {help_type!r}")

        current_id = session.get("current_id")
        question = Question.query.get(current_id) if current_id else None
        result_payload: dict[str, Any] = {}

        if help_type == "hint":
            if question is None:
                return json_error("No current question.")
            result_payload["hint"] = question.hint

        elif help_type == "remove_wrong":
            if question is None:
                return json_error("No current question.")
            indices = remove_two_wrong_indices(
                question.options, question.correct_answers)
            result_payload["remove_indices"] = indices

        elif help_type == "change_question":
            next_q = _load_next_question()
            if next_q is None:
                return json_error("No more questions to switch to.")
            result_payload["question"] = _question_for_student(next_q)

        elif help_type == "add_time":
            result_payload["extra_seconds"] = 60

        new_score = deduct_score(session, help_type)
        result_payload["score"] = new_score
        return json_ok(result_payload)

    # ── Finish & save ─────────────────────────────────────────────────────────

    @app.route("/finish_exam_student", methods=["POST"])
    @require_active_session
    def finish_exam_student():
        exam_state = get_session_state(session)
        return json_ok({
            "student_name": exam_state["student_name"],
            "final_score":  exam_state["score"],
            "helps_used":   exam_state["helps_used"],
            "time_taken":   exam_state["elapsed"],
        })

    @app.route("/save_final_score", methods=["POST"])
    def save_final_score():
        data = request.get_json(silent=True) or {}
        student_name = (data.get("student_name") or "").strip()
        try:
            final_score = float(data.get("final_score", 0))
        except (TypeError, ValueError):
            return json_error("Invalid score value.")
        if not student_name:
            return json_error("Student name is required.")

        result = StudentResult(student_name=student_name,
                               final_score=final_score)
        db.session.add(result)
        db.session.commit()
        clear_student_session(session)
        return json_ok({"saved": result.to_dict()})

    # ── Results CRUD ──────────────────────────────────────────────────────────

    @app.route("/get_student_results", methods=["GET"])
    def get_student_results():
        results = StudentResult.query.order_by(
            StudentResult.timestamp.desc()).all()
        return json_ok({"results": [r.to_dict() for r in results]})

    @app.route("/update_result/<int:result_id>", methods=["POST"])
    def update_result(result_id: int):
        result = StudentResult.query.get_or_404(result_id)
        data = request.get_json(silent=True) or {}
        if "student_name" in data:
            name = data["student_name"].strip()
            if not name:
                return json_error("Student name cannot be empty.")
            result.student_name = name
        if "final_score" in data:
            try:
                result.final_score = float(data["final_score"])
            except (TypeError, ValueError):
                return json_error("Invalid score value.")
        db.session.commit()
        return json_ok({"updated": result.to_dict()})

    @app.route("/delete_result/<int:result_id>", methods=["POST"])
    def delete_result(result_id: int):
        result = StudentResult.query.get_or_404(result_id)
        db.session.delete(result)
        db.session.commit()
        return json_ok({"deleted_id": result_id})

    # ── Excel export ──────────────────────────────────────────────────────────

    @app.route("/export_results", methods=["GET"])
    def export_results():
        """Export all student results as an Excel (.xlsx) file."""
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        except ImportError:
            return json_error(
                "openpyxl is not installed. Run: pip install openpyxl", 500
            )

        results = StudentResult.query.order_by(
            StudentResult.timestamp.asc()).all()

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Student Results"

        # ── Styles ────────────────────────────────────────────────────────────
        header_fill = PatternFill("solid", fgColor="2563EB")
        header_font = Font(bold=True, color="FFFFFF", size=12)
        center = Alignment(horizontal="center", vertical="center")
        left = Alignment(horizontal="left",   vertical="center")
        thin = Side(style="thin", color="D1D5DB")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        def score_fill(score: float) -> PatternFill:
            if score >= 8:
                return PatternFill("solid", fgColor="DCFCE7")  # green
            if score >= 5:
                return PatternFill("solid", fgColor="FEF9C3")  # yellow
            return PatternFill("solid", fgColor="FEE2E2")      # red

        # ── Header row ────────────────────────────────────────────────────────
        headers = ["#", "Student Name", "Score / 10", "Date & Time"]
        col_widths = [6, 30, 14, 22]
        for col, (header, width) in enumerate(zip(headers, col_widths), start=1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center
            cell.border = border
            ws.column_dimensions[cell.column_letter].width = width

        ws.row_dimensions[1].height = 24

        # ── Data rows ─────────────────────────────────────────────────────────
        for row_idx, result in enumerate(results, start=2):
            values = [
                row_idx - 1,
                result.student_name,
                result.final_score,
                result.timestamp.strftime("%Y-%m-%d %H:%M"),
            ]
            aligns = [center, left, center, center]
            for col, (value, align) in enumerate(zip(values, aligns), start=1):
                cell = ws.cell(row=row_idx, column=col, value=value)
                cell.alignment = align
                cell.border = border
                if col == 3:  # score column
                    cell.fill = score_fill(result.final_score)
                    cell.font = Font(bold=True, size=11)
            ws.row_dimensions[row_idx].height = 20

        # ── Summary row ───────────────────────────────────────────────────────
        if results:
            summary_row = len(results) + 3
            ws.cell(row=summary_row,     column=1,
                    value="Total Students").font = Font(bold=True)
            ws.cell(row=summary_row,     column=2, value=len(results))
            ws.cell(row=summary_row + 1, column=1,
                    value="Average Score").font = Font(bold=True)
            avg = sum(r.final_score for r in results) / len(results)
            ws.cell(row=summary_row + 1, column=2, value=round(avg, 2))

        # ── Freeze header row ─────────────────────────────────────────────────
        ws.freeze_panes = "A2"

        # ── Stream to client ──────────────────────────────────────────────────
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        from datetime import datetime
        filename = f"oralify_results_{
            datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        return send_file(
            buf,
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
