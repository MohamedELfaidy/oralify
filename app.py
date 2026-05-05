"""
app.py – Oralify Flask application entry point.

Run:
    flask run          (development)
    python app.py      (also development)
"""

from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, session

from exam_logic import (
    HELP_COSTS,
    INITIAL_SCORE,
    clear_student_session,
    deduct_score,
    elapsed_seconds,
    get_next_question_id,
    get_session_state,
    init_session,
    is_correct,
    remove_two_wrong_indices,
    reset_question_pool,
    set_score,
)
from models import Question, StudentResult, db
from question_processor import call_groq, extract_text, parse_groq_response

# ── Bootstrap ────────────────────────────────────────────────────────────────

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

    _register_routes(app)
    return app


# ── Helpers ──────────────────────────────────────────────────────────────────


def json_error(message: str, status: int = 400) -> tuple:
    return jsonify({"ok": False, "error": message}), status


def json_ok(payload: dict[str, Any] | None = None) -> tuple:
    response = {"ok": True}
    if payload:
        response.update(payload)
    return jsonify(response), 200


def require_active_session(f):
    """Decorator – return 400 when no exam session is active."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "student_name" not in session:
            return json_error("No active exam session.", 400)
        return f(*args, **kwargs)
    return wrapper


def _question_for_student(question: Question) -> dict:
    """Strip the correct_answer and hint before sending to the student."""
    return {
        "id": question.id,
        "text": question.text,
        "options": question.options,
    }


def _load_next_question():
    """
    Pick the next question not yet seen in this pool cycle.

    If every question has been shown the pool is automatically reset so
    the cycle restarts — questions are never repeated within a cycle.
    Returns the Question object, or None if no questions exist in the DB.
    """
    all_ids = [row.id for row in Question.query.with_entities(
        Question.id).all()]
    if not all_ids:
        return None

    asked: list[int] = session.get("asked_ids", [])
    remaining = [q_id for q_id in all_ids if q_id not in asked]

    # All questions exhausted → reset pool then try again
    if not remaining:
        reset_question_pool(session)

    next_id = get_next_question_id(session, all_ids)
    if next_id is None:
        return None

    return Question.query.get(next_id)


# ── Routes ───────────────────────────────────────────────────────────────────


def _register_routes(app: Flask) -> None:

    # ── Page ─────────────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template("index.html")

    # ── Teacher – upload & process ────────────────────────────────────────────

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
            logger.exception("Unexpected error during text extraction")
            return json_error(f"Text extraction failed: {exc}")

        try:
            raw_response = call_groq(raw_text)
            questions = parse_groq_response(raw_response)
        except EnvironmentError as exc:
            return json_error(str(exc))
        except ValueError as exc:
            return json_error(f"AI processing failed: {exc}")
        except Exception as exc:
            logger.exception("Unexpected Groq error")
            return json_error(f"AI service error: {exc}")

        return json_ok({"questions": questions})

    @app.route("/manual_process", methods=["POST"])
    def manual_process():
        data = request.get_json(silent=True) or {}
        raw_text = (data.get("text") or "").strip()
        if not raw_text:
            return json_error("No text provided.")

        try:
            raw_response = call_groq(raw_text)
            questions = parse_groq_response(raw_response)
        except EnvironmentError as exc:
            return json_error(str(exc))
        except Exception as exc:
            logger.exception("Error in manual_process")
            return json_error(f"AI processing failed: {exc}")

        return json_ok({"questions": questions})

    # ── Teacher – question CRUD ───────────────────────────────────────────────

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
            if q.get("correct_answer", "").strip() not in [o.strip() for o in opts]:
                return json_error(f"Question {idx + 1}: correct answer not in options list.")

        new_questions: list[Question] = []
        for q in questions_data:
            question = Question(
                text=q["text"].strip(),
                correct_answer=q["correct_answer"].strip(),
                hint=q.get("hint", "").strip(),
            )
            question.options = [o.strip() for o in q["options"]]
            new_questions.append(question)

        db.session.add_all(new_questions)
        db.session.commit()

        total = Question.query.count()
        return json_ok({"saved": len(new_questions), "total": total})

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
        if "correct_answer" in data:
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

    # ── Exam – session management ─────────────────────────────────────────────

    @app.route("/start_exam", methods=["POST"])
    def start_exam():
        """Start an exam for a student. Body: {name: str}"""
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return json_error("Student name is required.")

        if Question.query.count() == 0:
            return json_error("No questions in the database. Ask the teacher to add some.")

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
        Receive the student's answer choice. Always finishes the exam.
        Body: {selected_option: str, timed_out?: bool}

        Scoring rules:
          - Timed out            → 7  (fixed, regardless of correctness or helps)
          - Wrong answer         → 0
          - Correct answer       → 10 minus help deductions
        """
        data = request.get_json(silent=True) or {}
        selected = (data.get("selected_option") or "").strip()
        timed_out = bool(data.get("timed_out", False))

        if not selected:
            return json_error("No option selected.")

        current_id = session.get("current_id")
        if current_id is None:
            return json_error("No current question in session.")

        question = Question.query.get(current_id)
        if question is None:
            return json_error("Current question not found.")

        correct = is_correct(selected, question.correct_answer)
        time_taken = elapsed_seconds(session)

        if timed_out:
            # Time ran out → fixed score of 7, no matter what was selected
            set_score(session, 7.0)
        elif not correct:
            # Wrong answer → 0
            set_score(session, 0.0)
        # Correct answer → keep current score (10 minus any help deductions)

        exam_state = get_session_state(session)

        return json_ok({
            "correct":        correct,
            "correct_answer": question.correct_answer,
            "score":          exam_state["score"],
            "helps_used":     exam_state["helps_used"],
            "student_name":   exam_state["student_name"],
            "time_taken":     time_taken,
            "timed_out":      timed_out,
            "finished":       True,
        })

    # ── Exam – help system ────────────────────────────────────────────────────

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
                question.options, question.correct_answer)
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

    # ── Exam – finish & save ──────────────────────────────────────────────────

    @app.route("/finish_exam_student", methods=["POST"])
    @require_active_session
    def finish_exam_student():
        """Student quits early (before submitting). Returns state for professor."""
        exam_state = get_session_state(session)
        return json_ok({
            "student_name": exam_state["student_name"],
            "final_score":  exam_state["score"],
            "helps_used":   exam_state["helps_used"],
            "time_taken":   exam_state["elapsed"],
        })

    @app.route("/save_final_score", methods=["POST"])
    def save_final_score():
        """
        Professor confirms and saves the score.
        Body: {student_name: str, final_score: float}
        Clears the per-student session keys (question pool is preserved).
        """
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
        """Professor modifies a saved result. Body: {student_name?, final_score?}"""
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


# ── Entry point ───────────────────────────────────────────────────────────────

app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
