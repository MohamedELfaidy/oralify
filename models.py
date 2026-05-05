"""
models.py – SQLAlchemy database models for Oralify.
"""

from datetime import datetime, timezone
import json

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Question(db.Model):
    """A multiple-choice exam question with hint and correct answer."""

    __tablename__ = "questions"

    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=False)
    _options = db.Column("options", db.Text, nullable=False)  # JSON array stored as text
    correct_answer = db.Column(db.Text, nullable=False)
    hint = db.Column(db.Text, nullable=False, default="")

    # ── options property ────────────────────────────────────────────────

    @property
    def options(self) -> list[str]:
        return json.loads(self._options)

    @options.setter
    def options(self, value: list[str]) -> None:
        self._options = json.dumps(value, ensure_ascii=False)

    # ── serialisation ───────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "options": self.options,
            "correct_answer": self.correct_answer,
            "hint": self.hint,
        }

    def __repr__(self) -> str:  # pragma: no cover
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

    def __repr__(self) -> str:  # pragma: no cover
        return f"<StudentResult id={self.id} name={self.student_name!r} score={self.final_score}>"
