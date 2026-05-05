"""
exam_logic.py – Pure exam session logic.

All functions receive / return plain data structures (no Flask imports)
so they can be unit-tested without an application context.
"""

from __future__ import annotations

import random
import time
from typing import Any

# ── Score constants ──────────────────────────────────────────────────────────

INITIAL_SCORE: float = 10.0

HELP_COSTS: dict[str, float] = {
    "hint": 0.5,
    "remove_wrong": 1.0,
    "ask_ai": 1.0,
    "ask_friend": 1.0,
    "change_question": 1.0,
    "add_time": 1.0,
}


# ── Session key names (central definition avoids typos) ─────────────────────

KEY_NAME = "student_name"
KEY_SCORE = "score"
# list[int] – IDs shown across ALL students this server session
KEY_ASKED = "asked_ids"
KEY_CURRENT = "current_id"     # int | None
KEY_HELPS_USED = "helps_used"     # list[str] – help types used this student
KEY_START_TIME = "start_time"     # float – unix timestamp when question was shown


# ── Session initialisation ───────────────────────────────────────────────────


def init_session(session: dict, student_name: str) -> None:
    """Populate *session* for a new student exam."""
    session[KEY_NAME] = student_name.strip()
    session[KEY_SCORE] = INITIAL_SCORE
    session[KEY_CURRENT] = None
    session[KEY_HELPS_USED] = []
    session[KEY_START_TIME] = None
    # KEY_ASKED is intentionally NOT reset here – it persists across students
    # so the same question is never repeated until all have been used.


def reset_question_pool(session: dict) -> None:
    """Call when all questions have been exhausted – resets the seen-IDs list."""
    session[KEY_ASKED] = []


# ── Question selection ───────────────────────────────────────────────────────


def get_next_question_id(session: dict, all_ids: list[int]) -> int | None:
    """
    Return the ID of the next question that has NOT been shown to any student
    since the last pool reset.

    Returns None only when every question has already been shown (caller should
    call reset_question_pool() then retry).

    Side-effect: updates session[KEY_CURRENT], appends to session[KEY_ASKED],
    and records session[KEY_START_TIME].
    """
    asked: list[int] = session.get(KEY_ASKED, [])
    remaining = [q_id for q_id in all_ids if q_id not in asked]

    if not remaining:
        return None

    chosen_id = random.choice(remaining)
    asked.append(chosen_id)
    session[KEY_ASKED] = asked
    session[KEY_CURRENT] = chosen_id
    session[KEY_START_TIME] = time.time()
    return chosen_id


# ── Scoring ──────────────────────────────────────────────────────────────────


def deduct_score(session: dict, help_type: str) -> float:
    """
    Deduct the cost of *help_type* from the session score (floor 0).

    Returns the new score.

    Raises
    ------
    ValueError
        When *help_type* is not recognised.
    """
    cost = HELP_COSTS.get(help_type)
    if cost is None:
        raise ValueError(f"Unknown help type: {help_type!r}")

    current: float = session.get(KEY_SCORE, INITIAL_SCORE)
    new_score = max(0.0, current - cost)
    session[KEY_SCORE] = new_score

    # Record help usage (each type recorded once)
    used: list = session.get(KEY_HELPS_USED, [])
    if help_type not in used:
        used.append(help_type)
    session[KEY_HELPS_USED] = used

    return new_score


def set_score(session: dict, new_score: float) -> float:
    """Directly overwrite the session score (professor adjustment)."""
    clamped = max(0.0, float(new_score))
    session[KEY_SCORE] = clamped
    return clamped


# ── Answer evaluation ────────────────────────────────────────────────────────


def is_correct(selected_option: str, correct_answer: str) -> bool:
    return selected_option.strip() == correct_answer.strip()


# ── Wrong-answer removal ─────────────────────────────────────────────────────


def remove_two_wrong_indices(options: list[str], correct_answer: str) -> list[int]:
    """
    Return the indices of two wrong options to hide.
    Always preserves the correct answer.
    """
    wrong_indices = [
        idx for idx, opt in enumerate(options)
        if opt.strip() != correct_answer.strip()
    ]
    random.shuffle(wrong_indices)
    return wrong_indices[:2]


# ── Elapsed time ─────────────────────────────────────────────────────────────


def elapsed_seconds(session: dict) -> int:
    """Return how many whole seconds have passed since the question was shown."""
    start = session.get(KEY_START_TIME)
    if start is None:
        return 0
    return max(0, int(time.time() - start))


# ── Session state helpers ────────────────────────────────────────────────────


def get_session_state(session: dict) -> dict[str, Any]:
    """Return a safe, serialisable snapshot of the exam session."""
    return {
        "student_name": session.get(KEY_NAME),
        "score":        session.get(KEY_SCORE, INITIAL_SCORE),
        "current_id":   session.get(KEY_CURRENT),
        "helps_used":   session.get(KEY_HELPS_USED, []),
        "elapsed":      elapsed_seconds(session),
    }


def clear_student_session(session: dict) -> None:
    """
    Remove per-student keys from the Flask session.
    Deliberately keeps KEY_ASKED so the question pool is preserved
    across consecutive students.
    """
    for key in (KEY_NAME, KEY_SCORE, KEY_CURRENT, KEY_HELPS_USED, KEY_START_TIME):
        session.pop(key, None)
