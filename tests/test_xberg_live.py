"""Live round-trip against a real xberg server — skipped unless one is
reachable. On the proxy net that's the default http://xberg:8000; from a
machine that can reach one (set XBERG_BASE_URL). Exercises the
classical (tesseract) tier only — the vlm tier spends LLM tokens and is
verified manually at deploy time instead.
"""

from pathlib import Path

import httpx
import pytest
from mcp_server_kit import pdf as pdf_read

FIXTURES = Path(__file__).parent / "fixtures"
SCANNED = (FIXTURES / "scanned.pdf").read_bytes()


def _xberg_reachable() -> bool:
    base_url, _timeout = pdf_read._xberg_config()
    try:
        return httpx.get(f"{base_url}/health", timeout=5).status_code == 200
    except Exception:
        return False


@pytest.mark.skipif(not _xberg_reachable(), reason="no xberg server reachable")
def test_scanned_pdf_real_xberg_roundtrip():
    r = pdf_read.extract_pdf_text(SCANNED, ocr="auto")
    assert r["ocr_used"] is True
    assert r["ocr_pages"] == [1, 2]
    assert r["ocr_engine"].startswith("xberg:")
    # Digits survive OCR most reliably — key on them (same assertions the
    # in-process RapidOCR suite used, so this doubles as quality parity).
    assert "421.50" in r["text"]
    assert "7734" in r["text"]
