"""
exam_logic.py - Exam session logic for Oralify v2.
Works with ExamAttempt objects stored in the database (not Flask session).
The Flask session only holds: teacher_id / student_id / role.
"""

from __future__ import annotations

import random
import time
from typing import Any

# ── Default scoring ───────────────────────────────────────────────────────────

INITIAL_SCORE: float = 10.0
TIMEOUT_SCORE: float = 7.0

DEFAULT_HELP_COSTS: dict[str, float] = {
    "hint":            0.5,
    "remove_wrong":    1.0,
    "ask_ai":          1.0,
    "ask_friend":      1.0,
    "change_question": 1.0,
    "add_time":        1.0,
}

ALL_HELP_TYPES = list(DEFAULT_HELP_COSTS.keys())

# How many students must be examined before a question can repeat
COOLDOWN = 5


# ── Question pool with cooldown ───────────────────────────────────────────────

def pick_next_question_id(
    all_ids: list[int],
    asked_ids: list[int],
    history: list[int],
    count: int = 1,
) -> list[int]:
    """
    Pick *count* question IDs that:
    1. Have not been used yet in the current cycle (asked_ids).
    2. Avoid the last COOLDOWN entries in history where possible.

    Resets the cycle automatically when exhausted.
    Returns list of chosen IDs.
    """
    if not all_ids:
        return []

    remaining = [q for q in all_ids if q not in asked_ids]
    if not remaining:
        # Reset cycle
        asked_ids.clear()
        remaining = list(all_ids)

    recent = set(history[-COOLDOWN:]) if history else set()
    preferred = [q for q in remaining if q not in recent]
    pool = preferred if preferred else remaining

    chosen = random.sample(pool, min(count, len(pool)))
    # Fill remainder from full pool if needed
    if len(chosen) < count:
        rest = [q for q in remaining if q not in chosen]
        chosen += random.sample(rest, min(count - len(chosen), len(rest)))

    return chosen


# ── Multi-answer scoring ──────────────────────────────────────────────────────

def evaluate_answer(
    selected: list[str],
    correct_answers: list[str],
    current_score: float,
    timed_out: bool = False,
    help_costs: dict[str, float] | None = None,
) -> tuple[float, str]:
    """
    Returns (new_score, result_label).

    result_label: correct | wrong | all_correct | partial | all_wrong | timed_out
    """
    if timed_out:
        return TIMEOUT_SCORE, "timed_out"

    correct_set  = {c.strip() for c in correct_answers}
    selected_set = {s.strip() for s in selected}

    if len(correct_answers) == 1:
        if selected_set == correct_set:
            return current_score, "correct"
        return 0.0, "wrong"

    # Multi-answer
    wrong_chosen   = selected_set - correct_set
    correct_chosen = selected_set & correct_set

    if not wrong_chosen:
        return current_score, "all_correct"
    if correct_chosen:
        return max(0.0, current_score - 1.0), "partial"
    return max(0.0, current_score - 2.0), "all_wrong"


# ── Help deduction ────────────────────────────────────────────────────────────

def deduct_help(
    current_score: float,
    help_type: str,
    custom_costs: dict[str, float] | None = None,
) -> float:
    costs = {**DEFAULT_HELP_COSTS, **(custom_costs or {})}
    cost = costs.get(help_type, 1.0)
    return max(0.0, current_score - cost)


# ── Remove wrong options ──────────────────────────────────────────────────────

def remove_two_wrong_indices(options: list[str], correct_answers: list[str]) -> list[int]:
    correct_set = {c.strip() for c in correct_answers}
    wrong = [i for i, o in enumerate(options) if o.strip() not in correct_set]
    random.shuffle(wrong)
    return wrong[:2]
