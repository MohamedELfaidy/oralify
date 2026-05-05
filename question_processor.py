"""
question_processor.py – File text extraction and Groq API integration.

Responsibilities
----------------
- extract_text(file)        : detect file type and extract plain text
- call_groq(raw_text)       : send text to Groq, return raw JSON string
- parse_groq_response(json) : validate and return list[dict]
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Groq client (lazy-loaded so import never fails on missing key) ───────────

_groq_client = None


def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        from groq import Groq  # type: ignore

        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "GROQ_API_KEY environment variable is not set. "
                "Add it to your .env file."
            )
        _groq_client = Groq(api_key=api_key)
    return _groq_client


# ── Constants ────────────────────────────────────────────────────────────────

GROQ_MODEL = "llama-3.3-70b-versatile"

GROQ_SYSTEM_PROMPT = """\
You are an exam assistant. Convert the following text into multiple-choice questions.
For each question, provide:
- The question text
- An array of answer options (3 to 6 options)
- The correct answer (exact string matching one of the options)
- A short hint (one sentence)

Even if the original text does not contain answers, infer the correct answer based on common knowledge.
If the original contains essay questions, transform them into well-formed MCQs.

Output ONLY valid JSON with no extra text, preamble, or markdown fences, in this exact format:
{
  "questions": [
    {
      "text": "...",
      "options": ["opt1", "opt2", "opt3"],
      "correct_answer": "opt1",
      "hint": "..."
    }
  ]
}
"""


# ── Text extraction ──────────────────────────────────────────────────────────


def extract_text(file) -> str:
    """
    Accept a werkzeug FileStorage object and return extracted plain text.

    Supported formats: PDF, DOCX, PNG/JPG/JPEG/BMP/TIFF (via EasyOCR),
    and plain text files.

    Raises
    ------
    ValueError
        When the file type is not supported or extraction fails.
    """
    filename = file.filename or ""
    ext = Path(filename).suffix.lower()

    file_bytes = file.read()

    if ext == ".pdf":
        return _extract_pdf(file_bytes)
    elif ext in {".docx", ".doc"}:
        return _extract_docx(file_bytes)
    elif ext in {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}:
        return _extract_image_ocr(file_bytes)
    elif ext in {".txt", ".md", ""}:
        return file_bytes.decode("utf-8", errors="replace")
    else:
        raise ValueError(f"Unsupported file type: {ext!r}")


def _extract_pdf(data: bytes) -> str:
    import pdfplumber  # type: ignore

    text_parts: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)

    result = "\n\n".join(text_parts).strip()
    if not result:
        raise ValueError(
            "PDF contains no extractable text. Try uploading an image instead.")
    return result


def _extract_docx(data: bytes) -> str:
    from docx import Document  # type: ignore

    document = Document(io.BytesIO(data))
    paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
    result = "\n\n".join(paragraphs).strip()
    if not result:
        raise ValueError("DOCX file appears to be empty.")
    return result


def _extract_image_ocr(data: bytes) -> str:
    import easyocr  # type: ignore
    import numpy as np
    from PIL import Image  # type: ignore

    image = Image.open(io.BytesIO(data)).convert("RGB")
    image_array = np.array(image)

    # EasyOCR is slow to initialise; re-use the reader across calls
    reader = _get_ocr_reader()
    results = reader.readtext(image_array, detail=0, paragraph=True)
    result = "\n".join(results).strip()
    if not result:
        raise ValueError("OCR found no text in the image.")
    return result


_ocr_reader = None


def _get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is None:
        import easyocr  # type: ignore

        _ocr_reader = easyocr.Reader(["en"], gpu=False)
    return _ocr_reader


# ── Groq call ────────────────────────────────────────────────────────────────


def call_groq(raw_text: str) -> str:
    """
    Send *raw_text* to Groq and return the raw response string.

    The caller is responsible for parsing the JSON.
    """
    client = _get_groq_client()

    user_message = f"{GROQ_SYSTEM_PROMPT}\n\nRaw text:\n{raw_text}"

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {
                "role": "user",
                "content": user_message,
            }
        ],
        temperature=0.3,
        max_tokens=4096,
    )

    return response.choices[0].message.content


# ── Response parsing ─────────────────────────────────────────────────────────


def parse_groq_response(raw_response: str) -> list[dict[str, Any]]:
    """
    Parse and validate the JSON returned by Groq.

    Returns a list of question dicts with keys:
        text, options, correct_answer, hint

    Raises
    ------
    ValueError
        When the response is not valid JSON or missing required fields.
    """
    # Strip accidental markdown code fences if model adds them
    cleaned = re.sub(r"```(?:json)?|```", "", raw_response).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("Groq response is not valid JSON: %s", raw_response[:500])
        raise ValueError(f"Groq returned invalid JSON: {exc}") from exc

    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("Groq response contained no 'questions' array.")

    validated: list[dict[str, Any]] = []
    for idx, q in enumerate(questions):
        _validate_question(q, idx)
        validated.append(
            {
                "text": q["text"].strip(),
                "options": [str(o).strip() for o in q["options"]],
                "correct_answer": str(q["correct_answer"]).strip(),
                "hint": str(q.get("hint", "")).strip(),
            }
        )

    return validated


def _validate_question(q: dict, idx: int) -> None:
    required_fields = ("text", "options", "correct_answer")
    for field in required_fields:
        if field not in q:
            raise ValueError(
                f"Question {idx} is missing required field '{field}'.")

    if not isinstance(q["options"], list) or len(q["options"]) < 2:
        raise ValueError(f"Question {idx} must have at least 2 options.")

    if str(q["correct_answer"]).strip() not in [str(o).strip() for o in q["options"]]:
        raise ValueError(
            f"Question {idx}: correct_answer {q['correct_answer']!r} "
            "is not present in options."
        )
