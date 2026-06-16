from __future__ import annotations
from datetime import datetime, timezone
import json
import hashlib
import secrets
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def _now(): return datetime.now(timezone.utc)


def hash_password(p):
    s = secrets.token_hex(16)
    return f"{s}:{hashlib.sha256((s+p).encode()).hexdigest()}"


def verify_password(p, stored):
    try:
        s, h = stored.split(":", 1)
        return hashlib.sha256((s+p).encode()).hexdigest() == h
    except:
        return False


class Teacher(db.Model):
    __tablename__ = "teachers"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(255), nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=_now)
    exams = db.relationship(
        "Exam", back_populates="teacher", cascade="all, delete-orphan")

    def set_password(self, p): self.password_hash = hash_password(p)
    def check_password(self, p): return verify_password(p, self.password_hash)
    def to_dict(self): return {"id": self.id, "username": self.username,
                               "email": self.email, "full_name": self.full_name}


class Student(db.Model):
    __tablename__ = "students"
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.String(50), unique=True, nullable=False)
    full_name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_now)
    attempts = db.relationship("ExamAttempt", back_populates="student",
                               foreign_keys="ExamAttempt.student_id", cascade="all, delete-orphan")

    def set_password(self, p): self.password_hash = hash_password(p)
    def check_password(self, p): return verify_password(p, self.password_hash)
    def to_dict(self): return {"id": self.id, "student_id": self.student_id,
                               "full_name": self.full_name, "email": self.email}


class Exam(db.Model):
    __tablename__ = "exams"
    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey(
        "teachers.id"), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    course_name = db.Column(db.String(255), nullable=False, default="")
    course_code = db.Column(db.String(50), nullable=False, default="")
    description = db.Column(db.Text, nullable=False, default="")
    questions_per_student = db.Column(db.Integer, nullable=False, default=1)
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)
    _allowed_helps = db.Column("allowed_helps", db.Text, nullable=False,
                               default='["hint","remove_wrong","ask_ai","ask_friend","change_question","add_time"]')
    _help_costs = db.Column("help_costs", db.Text,
                            nullable=False, default="{}")
    created_at = db.Column(db.DateTime, nullable=False, default=_now)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    is_public = db.Column(db.Boolean, nullable=False, default=False)
    total_marks = db.Column(db.Float, nullable=False, default=10.0)
    _question_marks = db.Column(
        "question_marks", db.Text, nullable=False, default="{}")
    teacher = db.relationship("Teacher", back_populates="exams")
    questions = db.relationship("ExamQuestion", back_populates="exam",
                                cascade="all, delete-orphan", order_by="ExamQuestion.position")
    attempts = db.relationship(
        "ExamAttempt", back_populates="exam", cascade="all, delete-orphan")

    @property
    def allowed_helps(self): return json.loads(self._allowed_helps)
    @allowed_helps.setter
    def allowed_helps(self, v): self._allowed_helps = json.dumps(v)
    @property
    def help_costs(self): return json.loads(self._help_costs)
    @help_costs.setter
    def help_costs(self, v): self._help_costs = json.dumps(v)
    @property
    def question_marks(self): return json.loads(self._question_marks)
    @question_marks.setter
    def question_marks(self, v): self._question_marks = json.dumps(v)

    def marks_for_question(self, qid):
        qm = self.question_marks
        if str(qid) in qm:
            return float(qm[str(qid)])
        n = len(self.questions)
        return round(self.total_marks/n, 2) if n else self.total_marks

    @property
    def is_open(self):
        if not self.is_active:
            return False
        now = datetime.now()
        if self.start_time and now < self.start_time:
            return False
        if self.end_time and now > self.end_time:
            return False
        return True

    @property
    def status(self):
        if not self.is_active:
            return "inactive"
        now = datetime.now()
        if self.start_time and now < self.start_time:
            return "upcoming"
        if self.end_time and now > self.end_time:
            return "ended"
        return "open"

    def to_dict(self, include_questions=False):
        d = {"id": self.id, "name": self.name, "course_name": self.course_name,
             "course_code": self.course_code, "description": self.description,
             "questions_per_student": self.questions_per_student,
             "start_time": self.start_time.isoformat() if self.start_time else None,
             "end_time": self.end_time.isoformat() if self.end_time else None,
             "allowed_helps": self.allowed_helps, "help_costs": self.help_costs,
             "is_active": self.is_active, "is_public": self.is_public,
             "total_marks": self.total_marks, "question_marks": self.question_marks,
             "status": self.status, "question_count": len(self.questions),
             "attempt_count": len(self.attempts),
             "teacher_name": self.teacher.full_name if self.teacher else "",
             "teacher_username": self.teacher.username if self.teacher else "",
             "created_at": self.created_at.isoformat()}
        if include_questions:
            d["questions"] = [q.to_dict(reveal=True) for q in self.questions]
        return d


class ExamQuestion(db.Model):
    __tablename__ = "exam_questions"
    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id"), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    text = db.Column(db.Text, nullable=False)
    _options = db.Column("options", db.Text, nullable=False)
    _correct_answers = db.Column("correct_answers", db.Text, nullable=False)
    hint = db.Column(db.Text, nullable=False, default="")
    timer_seconds = db.Column(db.Integer, nullable=True)
    exam = db.relationship("Exam", back_populates="questions")

    @property
    def options(self): return json.loads(self._options)
    @options.setter
    def options(self, v): self._options = json.dumps(v, ensure_ascii=False)
    @property
    def correct_answers(self): return json.loads(self._correct_answers)

    @correct_answers.setter
    def correct_answers(self, v): self._correct_answers = json.dumps(
        v, ensure_ascii=False)

    @property
    def correct_answer(self):
        a = self.correct_answers
        return a[0] if a else ""

    def to_dict(self, reveal=False):
        d = {"id": self.id, "text": self.text, "options": self.options, "hint": self.hint,
             "timer_seconds": self.timer_seconds,
             "multi_answer": len(self.correct_answers) > 1, "num_correct": len(self.correct_answers)}
        if reveal:
            d["correct_answers"] = self.correct_answers
        return d


class ExamAttempt(db.Model):
    __tablename__ = "exam_attempts"
    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey(
        "students.id"), nullable=True)
    guest_name = db.Column(db.String(255), nullable=True)
    _assigned_question_ids = db.Column(
        "assigned_question_ids", db.Text, nullable=False, default="[]")
    current_question_index = db.Column(db.Integer, nullable=False, default=0)
    _results = db.Column("results", db.Text, nullable=False, default="[]")
    final_score = db.Column(db.Float, nullable=True)
    helps_used = db.Column(db.Text, nullable=False, default="[]")
    started_at = db.Column(db.DateTime, nullable=False, default=_now)
    completed_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="in_progress")
    score_override = db.Column(db.Float, nullable=True)
    exam = db.relationship("Exam", back_populates="attempts")
    student = db.relationship(
        "Student", back_populates="attempts", foreign_keys=[student_id])

    @property
    def assigned_question_ids(self): return json.loads(
        self._assigned_question_ids)

    @assigned_question_ids.setter
    def assigned_question_ids(
        self, v): self._assigned_question_ids = json.dumps(v)

    @property
    def results(self): return json.loads(self._results)
    @results.setter
    def results(self, v): self._results = json.dumps(v)

    @property
    def display_score(self):
        if self.score_override is not None:
            return self.score_override
        return self.final_score

    @property
    def participant_name(self):
        if self.student:
            return self.student.full_name
        return self.guest_name or "Guest"

    @property
    def participant_id(self):
        if self.student:
            return self.student.student_id
        return "guest"

    def to_dict(self, include_results=False):
        return {"id": self.id, "exam_id": self.exam_id,
                "exam_name": self.exam.name if self.exam else "",
                "student_id": self.participant_id, "student_name": self.participant_name,
                "is_guest": self.student_id is None,
                "final_score": self.display_score, "raw_score": self.final_score,
                "score_override": self.score_override,
                "total_marks": self.exam.total_marks if self.exam else 10,
                "status": self.status, "helps_used": json.loads(self.helps_used),
                "started_at": self.started_at.strftime("%Y-%m-%d %H:%M") if self.started_at else None,
                "completed_at": self.completed_at.strftime("%Y-%m-%d %H:%M") if self.completed_at else None,
                "question_count": len(self.assigned_question_ids),
                "results": self.results if include_results else []}
