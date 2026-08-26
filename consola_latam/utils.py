from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from pathlib import Path


CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")


def clean_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ")
    if "Ã" in text or "Â" in text:
        for encoding in ("latin1", "cp1252"):
            try:
                text = text.encode(encoding).decode("utf-8")
                break
            except UnicodeError:
                continue
    text = CONTROL_CHARS_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_for_match(value: str) -> str:
    text = clean_text(value).upper()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text


def build_output_filename(sequence: int, day: str | date | datetime | None = None) -> str:
    if day is None:
        d = date.today()
    elif isinstance(day, datetime):
        d = day.date()
    elif isinstance(day, date):
        d = day
    else:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    return f"Consulta {sequence:02d} {d:%d-%m-%Y}.xlsx"


def next_output_path(output_dir: Path, day: str | date | datetime | None = None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    sequence = 1
    while True:
        path = output_dir / build_output_filename(sequence, day)
        if not path.exists():
            return path
        sequence += 1
