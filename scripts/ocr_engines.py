#!/usr/bin/env python3
"""Pluggable OCR engines for receipt ingestion.

Every engine implements a single method::

    extract_text(image_path: Path) -> str

The default is pytesseract (local, offline). Additional engines (easyocr,
Claude vision) can be registered without touching the ingest logic.

Privacy note: local engines (pytesseract, easyocr) never transmit the image.
A Claude-vision engine WOULD send the image externally and is therefore
opt-in only — see docs/PLANNING.md.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict


class OCREngine:
    """Base OCR engine interface."""

    name = "base"

    def extract_text(self, image_path: Path) -> str:  # pragma: no cover
        raise NotImplementedError


class PytesseractEngine(OCREngine):
    """Local OCR via Tesseract. Offline; no image leaves the machine."""

    name = "pytesseract"

    def __init__(self, lang: str = "ind+eng") -> None:
        self.lang = lang

    def extract_text(self, image_path: Path) -> str:
        try:
            import pytesseract
            from PIL import Image
        except ImportError as e:
            raise SystemExit(
                "pytesseract/Pillow not installed. Install with:\n"
                "  pip install pytesseract pillow\n"
                "and the Tesseract binary:\n"
                "  macOS:  brew install tesseract tesseract-lang\n"
                "  Ubuntu: sudo apt-get install tesseract-ocr tesseract-ocr-ind"
            ) from e

        try:
            with Image.open(image_path) as img:
                return pytesseract.image_to_string(img, lang=self.lang)
        except pytesseract.TesseractNotFoundError as e:
            raise SystemExit(
                "Tesseract binary not found on PATH. Install it:\n"
                "  macOS:  brew install tesseract tesseract-lang\n"
                "  Ubuntu: sudo apt-get install tesseract-ocr tesseract-ocr-ind"
            ) from e


class EasyOCREngine(OCREngine):
    """Local OCR via easyocr. Heavier (PyTorch) but better on messy photos."""

    name = "easyocr"

    def __init__(self, langs: tuple[str, ...] = ("id", "en")) -> None:
        self.langs = list(langs)
        self._reader = None

    def extract_text(self, image_path: Path) -> str:
        try:
            import easyocr
        except ImportError as e:
            raise SystemExit(
                "easyocr not installed. Install with: pip install easyocr"
            ) from e
        if self._reader is None:
            self._reader = easyocr.Reader(self.langs)
        lines = self._reader.readtext(str(image_path), detail=0, paragraph=True)
        return "\n".join(lines)


# name -> factory
_ENGINES: Dict[str, Callable[[], OCREngine]] = {
    "pytesseract": PytesseractEngine,
    "easyocr": EasyOCREngine,
}

DEFAULT_ENGINE = "pytesseract"


def get_engine(name: str = DEFAULT_ENGINE) -> OCREngine:
    if name not in _ENGINES:
        raise SystemExit(
            f"Unknown OCR engine {name!r}. Available: {sorted(_ENGINES)}"
        )
    return _ENGINES[name]()
