from pathlib import Path

from pypdf import PdfReader


def extract_text_from_pdf(filepath: str | Path) -> str:
    """
    Extracts text from a PDF file.
    """
    reader = PdfReader(filepath)
    text = ""
    for page in reader.pages:
        text += page.extract_text() + "\n"
    return text


def read_text_file(filepath: str | Path) -> str:
    """Reads content from a text file."""
    with open(filepath, encoding="utf-8") as f:
        return f.read()
