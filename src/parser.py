from pathlib import Path
from typing import BinaryIO

from runtime.pdf_text import extract_pdf_text


def extract_text_from_pdf(filepath: str | Path | BinaryIO) -> str:
    """
    Extracts text from a PDF file.
    """
    text = extract_pdf_text(filepath)
    return f"{text}\n" if text else ""


def read_text_file(filepath: str | Path) -> str:
    """Reads content from a text file."""
    with open(filepath, encoding="utf-8") as f:
        return f.read()
