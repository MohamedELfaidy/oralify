"""
question_processor.py - File text extraction and Groq API integration.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GROQ_MODEL = "llama-3.3-70b-versatile"

GROQ_SYSTEM_PROMPT = """\
You are an exam assistant. Convert the following text into multiple-choice questions.
For each question, provide:
- The question text
- An array of answer options (3 to 6 options)
- correct_answers: a JSON array of strings. Use ONE correct answer for regular questions,
  or TWO correct answers for questions where multiple answers apply.
- A short hint (one sentence)
- timer_seconds: optional integer (e.g. 60). Leave null to use the exam default.

Output ONLY valid JSON with no extra text, preamble, or markdown fences:
{
  "questions": [
    {
      "text": "...",
      "options": ["opt1", "opt2", "opt3"],
      "correct_answers": ["opt1"],
      "hint": "...",
      "timer_seconds": null
    }
  ]
}

Raw text:
"""

_groq_client = None


def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "GROQ_API_KEY environment variable is not set.")
        _groq_client = Groq(api_key=api_key)
    return _groq_client


def extract_text(file) -> str:
    filename = file.filename or ""
    ext = Path(filename).suffix.lower()
    data = file.read()

    if ext == ".pdf":
        return _extract_pdf(data)
    elif ext in {".docx", ".doc"}:
        return _extract_docx(data)
    elif ext in {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}:
        return _extract_image_ocr(data)
    elif ext in {".txt", ".md", ""}:
        return data.decode("utf-8", errors="replace")
    raise ValueError(f"Unsupported file type: {ext!r}")


def _extract_pdf(data: bytes) -> str:
    import pdfplumber
    parts = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                parts.append(t)
    result = "\n\n".join(parts).strip()
    if not result:
        raise ValueError("PDF contains no extractable text.")
    return result


def _extract_docx(data: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(data))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    result = "\n\n".join(paragraphs).strip()
    if not result:
        raise ValueError("DOCX file appears to be empty.")
    return result


_ocr_reader = None


def _extract_image_ocr(data: bytes) -> str:
    import easyocr
    import numpy as np
    from PIL import Image

    global _ocr_reader
    if _ocr_reader is None:
        _ocr_reader = easyocr.Reader(["en"], gpu=False)

    image = Image.open(io.BytesIO(data)).convert("RGB")
    results = _ocr_reader.readtext(np.array(image), detail=0, paragraph=True)
    result = "\n".join(results).strip()
    if not result:
        raise ValueError("OCR found no text in the image.")
    return result


def call_groq(raw_text: str, instructions: str = "") -> str:
    client = _get_groq_client()
    instructions_block = (
        f"\n\nAdditional instructions from the teacher (follow these carefully):\n{
            instructions}"
        if instructions else ""
    )
    full_prompt = GROQ_SYSTEM_PROMPT + raw_text + instructions_block
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": full_prompt}],
        temperature=0.3,
        max_tokens=4096,
    )
    return response.choices[0].message.content


def parse_groq_response(raw: str) -> list[dict[str, Any]]:
    cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Groq returned invalid JSON: {e}") from e

    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("Groq response contained no 'questions' array.")

    validated = []
    for idx, q in enumerate(questions):
        if "text" not in q or "options" not in q:
            raise ValueError(f"Question {idx} missing required fields.")

        # Support both correct_answers (list) and correct_answer (string)
        correct = q.get("correct_answers") or (
            [q["correct_answer"]] if q.get("correct_answer") else []
        )
        if not correct:
            raise ValueError(f"Question {idx} has no correct answer.")

        opts = [str(o).strip() for o in q["options"]]
        for c in correct:
            if c.strip() not in opts:
                raise ValueError(f"Question {idx}: correct answer '{
                                 c}' not in options.")

        validated.append({
            "text":            q["text"].strip(),
            "options":         opts,
            "correct_answers": [c.strip() for c in correct],
            "hint":            str(q.get("hint", "")).strip(),
            "timer_seconds":   q.get("timer_seconds"),
        })

    return validated
