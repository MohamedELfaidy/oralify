"""
exam_logic.py – Pure exam session logic.
"""

from __future__ import annotations

import random
import time
from typing import Any

# ── Score constants ───────────────────────────────────────────────────────────

INITIAL_SCORE: float = 10.0

HELP_COSTS: dict[str, float] = {
    "hint": 0.5,
    "remove_wrong": 1.0,
    "ask_ai": 1.0,
    "ask_friend": 1.0,
    "change_question": 1.0,
    "add_time": 1.0,
}

# ── Session key names ─────────────────────────────────────────────────────────

KEY_NAME = "student_name"
KEY_SCORE = "score"
# list[int] – question IDs used in the current cycle
KEY_ASKED = "asked_ids"
KEY_HISTORY = "asked_history"    # list[int] – recent IDs for cooldown (last N)
KEY_CURRENT = "current_id"
KEY_HELPS_USED = "helps_used"
KEY_START_TIME = "start_time"

# A question won't be re-shown until at least this many OTHER questions
# have been asked after it. Keeps re-use spread across many students.
COOLDOWN = 5


# ── Session initialisation ────────────────────────────────────────────────────


def init_session(session: dict, student_name: str) -> None:
    """Populate per-student keys. Question-pool keys are deliberately preserved."""
    session[KEY_NAME] = student_name.strip()
    session[KEY_SCORE] = INITIAL_SCORE
    session[KEY_CURRENT] = None
    session[KEY_HELPS_USED] = []
    session[KEY_START_TIME] = None


def reset_question_pool(session: dict) -> None:
    """Wipe the current-cycle asked list (all questions have been used once)."""
    session[KEY_ASKED] = []


# ── Smart question selection ──────────────────────────────────────────────────


def get_next_question_id(session: dict, all_ids: list[int]) -> int | None:
    """
    Pick the next question using a cooldown-aware strategy:

    1. Prefer questions not yet shown in this cycle (KEY_ASKED).
    2. Among those, exclude the last COOLDOWN questions from KEY_HISTORY
       so recently-seen questions are avoided even after a full pool reset.
    3. If all remaining candidates are in the cooldown window, fall back to
       any unused question regardless of cooldown.
    4. Caller is responsible for resetting the pool before calling when
       all_ids are exhausted from KEY_ASKED.

    Side-effects: updates KEY_CURRENT, KEY_ASKED, KEY_HISTORY, KEY_START_TIME.
    """
    asked:   list[int] = session.get(KEY_ASKED, [])
    history: list[int] = session.get(KEY_HISTORY, [])

    # Candidates: not yet used in current cycle
    remaining = [q for q in all_ids if q not in asked]
    if not remaining:
        return None  # caller must reset pool first

    # Prefer candidates outside the recent cooldown window
    recent = set(history[-COOLDOWN:]) if history else set()
    preferred = [q for q in remaining if q not in recent]
    candidates = preferred if preferred else remaining

    chosen_id = random.choice(candidates)

    # Update asked list
    asked.append(chosen_id)
    session[KEY_ASKED] = asked

    # Update history (keep last COOLDOWN * 2 entries so it doesn't grow forever)
    history.append(chosen_id)
    session[KEY_HISTORY] = history[-(COOLDOWN * 2):]

    session[KEY_CURRENT] = chosen_id
    session[KEY_START_TIME] = time.time()
    return chosen_id


# ── Scoring ───────────────────────────────────────────────────────────────────


def deduct_score(session: dict, help_type: str) -> float:
    cost = HELP_COSTS.get(help_type)
    if cost is None:
        raise ValueError(f"Unknown help type: {help_type!r}")

    current: float = session.get(KEY_SCORE, INITIAL_SCORE)
    new_score = max(0.0, current - cost)
    session[KEY_SCORE] = new_score

    used: list = session.get(KEY_HELPS_USED, [])
    if help_type not in used:
        used.append(help_type)
    session[KEY_HELPS_USED] = used

    return new_score


def set_score(session: dict, new_score: float) -> float:
    clamped = max(0.0, float(new_score))
    session[KEY_SCORE] = clamped
    return clamped


# ── Multi-answer evaluation ───────────────────────────────────────────────────


def evaluate_multi_answer(
    selected: list[str],
    correct_answers: list[str],
    current_score: float,
) -> tuple[float, str]:
    """
    Evaluate a student's selection against possibly multiple correct answers.

    Rules
    -----
    - Single correct answer question  → behaves as before (correct = full, wrong = 0)
    - Multi correct answer question (exactly 2 correct options):
        • Both selected are correct  → keep current score (full marks)
        • One correct + one wrong    → deduct 1 mark
        • Both selected are wrong    → deduct 2 marks
        • Timed-out / no selection   → handled by caller before this function

    Returns (new_score, result_label)
    result_label: "all_correct" | "partial" | "all_wrong" | "correct" | "wrong"
    """
    correct_set = set(c.strip() for c in correct_answers)
    selected_set = set(s.strip() for s in selected)

    # Single-answer question
    if len(correct_answers) == 1:
        if selected_set == correct_set:
            return current_score, "correct"
        else:
            return 0.0, "wrong"

    # Multi-answer question
    correct_chosen = selected_set & correct_set
    wrong_chosen = selected_set - correct_set

    if len(wrong_chosen) == 0:
        # All chosen answers are correct
        return current_score, "all_correct"
    elif len(correct_chosen) >= 1 and len(wrong_chosen) >= 1:
        # Mixed: at least one right, at least one wrong
        new_score = max(0.0, current_score - 1.0)
        return new_score, "partial"
    else:
        # All chosen are wrong
        new_score = max(0.0, current_score - 2.0)
        return new_score, "all_wrong"


# ── Wrong-answer removal ──────────────────────────────────────────────────────


def remove_two_wrong_indices(options: list[str], correct_answers: list[str]) -> list[int]:
    """Return indices of two wrong options to hide. Always preserves all correct answers."""
    correct_set = set(c.strip() for c in correct_answers)
    wrong_indices = [
        idx for idx, opt in enumerate(options)
        if opt.strip() not in correct_set
    ]
    random.shuffle(wrong_indices)
    return wrong_indices[:2]


# ── Elapsed time ──────────────────────────────────────────────────────────────


def elapsed_seconds(session: dict) -> int:
    start = session.get(KEY_START_TIME)
    if start is None:
        return 0
    return max(0, int(time.time() - start))


# ── Session state helpers ─────────────────────────────────────────────────────


def get_session_state(session: dict) -> dict[str, Any]:
    return {
        "student_name": session.get(KEY_NAME),
        "score":        session.get(KEY_SCORE, INITIAL_SCORE),
        "current_id":   session.get(KEY_CURRENT),
        "helps_used":   session.get(KEY_HELPS_USED, []),
        "elapsed":      elapsed_seconds(session),
    }


def clear_student_session(session: dict) -> None:
    """
    Remove per-student keys only.
    KEY_ASKED, KEY_HISTORY stay intact so the cooldown pool persists.
    """
    for key in (KEY_NAME, KEY_SCORE, KEY_CURRENT, KEY_HELPS_USED, KEY_START_TIME):
        session.pop(key, None)
