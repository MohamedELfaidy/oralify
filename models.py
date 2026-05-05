"""
models.py – SQLAlchemy database models for Oralify.
"""

from datetime import datetime, timezone
import json

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Question(db.Model):
    """
    A multiple-choice exam question.

    Supports both single and multiple correct answers.
    correct_answers is a JSON list of strings; each must match one of the options.
    The legacy `correct_answer` column is kept as a computed alias for the first
    correct answer so old data still works.
    """

    __tablename__ = "questions"

    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=False)
    _options = db.Column("options", db.Text, nullable=False)
    _correct_answers = db.Column("correct_answers", db.Text, nullable=False)
    hint = db.Column(db.Text, nullable=False, default="")

    # ── options ──────────────────────────────────────────────────────────

    @property
    def options(self) -> list[str]:
        return json.loads(self._options)

    @options.setter
    def options(self, value: list[str]) -> None:
        self._options = json.dumps(value, ensure_ascii=False)

    # ── correct_answers ───────────────────────────────────────────────────

    @property
    def correct_answers(self) -> list[str]:
        return json.loads(self._correct_answers)

    @correct_answers.setter
    def correct_answers(self, value: list[str]) -> None:
        self._correct_answers = json.dumps(value, ensure_ascii=False)

    # Legacy single-answer alias (first correct answer)
    @property
    def correct_answer(self) -> str:
        answers = self.correct_answers
        return answers[0] if answers else ""

    @correct_answer.setter
    def correct_answer(self, value: str) -> None:
        """Allow setting via single string for backwards compatibility."""
        self._correct_answers = json.dumps([value.strip()], ensure_ascii=False)

    # ── serialisation ─────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "options": self.options,
            "correct_answers": self.correct_answers,
            "hint": self.hint,
            "multi_answer": len(self.correct_answers) > 1,
        }

    def __repr__(self) -> str:
        return f"<Question id={self.id} text={self.text[:40]!r}>"


class StudentResult(db.Model):
    """Final saved score for one student's exam session."""

    __tablename__ = "student_results"

    id = db.Column(db.Integer, primary_key=True)
    student_name = db.Column(db.String(255), nullable=False)
    final_score = db.Column(db.Float, nullable=False)
    timestamp = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "student_name": self.student_name,
            "final_score": self.final_score,
            "timestamp": self.timestamp.strftime("%Y-%m-%d %H:%M"),
        }
