"""Tests for google_accounts_mcp.pdf_read — the shared PDF text/OCR module.

Fixture provenance (tests/fixtures/, regenerate with make_ocr_fixtures.py):
  scanned.pdf — 2 image-only pages (no text layer at all)
  mixed.pdf   — page 1 native text, page 2 image-only
  sample.pdf  — 2 native-text pages (from make_fixture.py)

OCR tests mock the xberg HTTP API (no network) — the request shape and the
per-page result mapping are asserted here; a real round-trip lives in
test_xberg_live.py (skipped unless an xberg server is reachable).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from google_accounts_mcp import pdf_read

FIXTURES = Path(__file__).parent / "fixtures"
SCANNED = (FIXTURES / "scanned.pdf").read_bytes()
MIXED = (FIXTURES / "mixed.pdf").read_bytes()
SAMPLE = (FIXTURES / "sample.pdf").read_bytes()


# ===================================================================
# validate_options
# ===================================================================


class TestValidateOptions:
    def test_defaults_normalise(self):
        assert pdf_read.validate_options("text", "auto") == ("text", "auto")
        assert pdf_read.validate_options("", "") == ("text", "auto")
        assert pdf_read.validate_options(" LAYOUT ", " OFF ") == (
            "layout", "off",
        )

    def test_bad_mode_raises(self):
        with pytest.raises(ValueError, match="mode must be one of"):
            pdf_read.validate_options("nope", "auto")

    def test_bad_ocr_raises(self):
        with pytest.raises(ValueError, match="ocr must be one of"):
            pdf_read.validate_options("text", "tesseract")


# ===================================================================
# xberg OCR (HTTP mocked)
# ===================================================================


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeHttpClient:
    """Stands in for httpx.Client — records posts, returns queued responses."""

    instances: list["_FakeHttpClient"] = []

    def __init__(self, *a, **kw):
        self.kwargs = kw
        self.posts: list[tuple[str, dict, list]] = []
        self.responses: list[_FakeResponse] = []
        _FakeHttpClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, data=None, files=None):
        self.posts.append((url, data, files))
        return self.responses.pop(0)


def _xberg_response(*texts: str) -> _FakeResponse:
    return _FakeResponse(payload={
        "results": [
            {"content": t, "metadata": {"ocr_used": True}} for t in texts
        ],
        "summary": {
            "inputs": len(texts), "results": len(texts), "errors": 0,
        },
    })


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("XBERG_BASE_URL", "XBERG_TIMEOUT", "XBERG_OCR_BACKEND",
                "XBERG_VLM_MODEL", "XBERG_VLM_BASE_URL",
                "XBERG_VLM_API_KEY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fake_http(clean_env, monkeypatch):
    _FakeHttpClient.instances = []

    def queue(*responses):
        def client_factory(*a, **kw):
            c = _FakeHttpClient(*a, **kw)
            c.responses = list(responses)
            return c

        monkeypatch.setattr("httpx.Client", client_factory)

    return queue


class TestXbergOcr:
    def test_scanned_ocr_off_yields_no_text(self):
        r = pdf_read.extract_pdf_text(SCANNED, ocr="off")
        assert r["text"].strip() == ""
        assert "ocr_used" not in r
        assert "ocr_error" not in r

    def test_scanned_auto_ocrs_all_pages(self, fake_http):
        fake_http(_xberg_response("Total due 421.50", "Ref 7734"))
        r = pdf_read.extract_pdf_text(SCANNED, ocr="auto")
        assert r["ocr_used"] is True
        assert r["ocr_pages"] == [1, 2]
        assert r["ocr_engine"] == "xberg:tesseract"
        assert "421.50" in r["text"]
        assert "7734" in r["text"]

        # ONE multipart request carrying both pages + the classical config.
        client = _FakeHttpClient.instances[-1]
        assert len(client.posts) == 1
        url, data, files = client.posts[0]
        assert url == "http://xberg:8000/extract"
        assert data["output_format"] == "markdown"
        cfg = json.loads(data["config"])
        assert cfg["ocr"]["backend"] == "tesseract"
        assert cfg["ocr"]["language"] == "eng"
        assert [f[1][0] for f in files] == ["page-1.png", "page-2.png"]
        assert all(f[1][1][:8] == b"\x89PNG\r\n\x1a\n" for f in files)

    def test_mixed_auto_ocrs_only_image_page(self, fake_http):
        fake_http(_xberg_response("Zip 55901"))
        r = pdf_read.extract_pdf_text(MIXED, ocr="auto")
        assert r["ocr_pages"] == [2]
        # Page 1 native text preserved, page 2 OCR'd.
        assert "Native total: $10.00" in r["text"]
        assert "55901" in r["text"]

    def test_text_pdf_auto_triggers_no_ocr(self, monkeypatch):
        def boom(*a, **kw):
            raise AssertionError("OCR must not run on a text PDF")

        monkeypatch.setattr(pdf_read, "xberg_ocr_pages", boom)
        r = pdf_read.extract_pdf_text(SAMPLE, ocr="auto")
        assert "ocr_used" not in r
        assert "Invoice total: $42.50" in r["text"]

    def test_force_ocrs_every_page(self, fake_http):
        fake_http(_xberg_response("FORCED", "FORCED"))
        r = pdf_read.extract_pdf_text(SAMPLE, ocr="force")
        assert r["ocr_pages"] == [1, 2]
        # Native text discarded — OCR output only.
        assert "Invoice total" not in r["text"]
        assert r["text"].count("FORCED") == 2

    def test_force_respects_page_spec(self, fake_http):
        fake_http(_xberg_response("FORCED"))
        r = pdf_read.extract_pdf_text(SAMPLE, pages="2", ocr="force")
        assert r["ocr_pages"] == [2]
        assert r["pages_returned"] == [2]
        _url, _data, files = _FakeHttpClient.instances[-1].posts[0]
        assert [f[1][0] for f in files] == ["page-2.png"]

    def test_base_url_env_override(self, fake_http, monkeypatch):
        monkeypatch.setenv("XBERG_BASE_URL", "http://ocr.example:9999/")
        fake_http(_xberg_response("A", "B"))
        pdf_read.extract_pdf_text(SCANNED, ocr="force")
        url, _data, _files = _FakeHttpClient.instances[-1].posts[0]
        # Trailing slash stripped, /extract appended.
        assert url == "http://ocr.example:9999/extract"

    def test_auto_broken_ocr_stack_reports_not_raises(
        self, clean_env, monkeypatch,
    ):
        def boom(*a, **kw):
            raise RuntimeError("xberg unreachable")

        monkeypatch.setattr(pdf_read, "_render_pages_png", boom)
        r = pdf_read.extract_pdf_text(SCANNED, ocr="auto")
        assert "ocr_used" not in r
        assert "xberg unreachable" in r["ocr_error"]
        assert r["text"].strip() == ""  # native result still returned

    def test_force_broken_ocr_stack_raises(self, clean_env, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("xberg unreachable")

        monkeypatch.setattr(pdf_read, "_render_pages_png", boom)
        with pytest.raises(RuntimeError, match="xberg unreachable"):
            pdf_read.extract_pdf_text(SCANNED, ocr="force")

    def test_http_error_surfaces_status_and_body(self, fake_http):
        fake_http(_FakeResponse(status_code=413, text="payload too large"))
        with pytest.raises(RuntimeError, match="HTTP 413.*payload too large"):
            pdf_read.extract_pdf_text(SCANNED, ocr="force")

    def test_extraction_errors_raise(self, fake_http):
        fake_http(_FakeResponse(payload={
            "results": [{"content": ""}],
            "summary": {"inputs": 2, "results": 1, "errors": 1},
        }))
        with pytest.raises(RuntimeError, match="1 extraction error"):
            pdf_read.extract_pdf_text(SCANNED, ocr="force")

    def test_result_count_mismatch_raises(self, fake_http):
        fake_http(_xberg_response("only one result"))
        with pytest.raises(RuntimeError, match="refusing to guess"):
            pdf_read.extract_pdf_text(SCANNED, ocr="force")


class TestVlmOcr:
    def test_unconfigured_raises_actionable_error(self, clean_env):
        with pytest.raises(RuntimeError, match="XBERG_VLM_API_KEY"):
            pdf_read.extract_pdf_text(SCANNED, ocr="llm")

    def test_llm_transcribes_pages_via_vlm_backend(
        self, fake_http, monkeypatch,
    ):
        monkeypatch.setenv("XBERG_VLM_API_KEY", "sekret")
        fake_http(_xberg_response("PAGE ONE TEXT", "PAGE TWO TEXT"))
        r = pdf_read.extract_pdf_text(SCANNED, ocr="llm")

        assert r["ocr_used"] is True
        assert r["ocr_engine"] == "xberg:vlm:cheap"
        assert r["ocr_pages"] == [1, 2]
        assert "PAGE ONE TEXT" in r["text"]
        assert "PAGE TWO TEXT" in r["text"]

        url, data, files = _FakeHttpClient.instances[-1].posts[0]
        assert url == "http://xberg:8000/extract"
        cfg = json.loads(data["config"])
        assert cfg["ocr"]["backend"] == "vlm"
        # Model is the LiteLLM alias verbatim — xberg forwards it unparsed
        # (an 'openai/' prefix would 400 at the gateway).
        assert cfg["ocr"]["vlm_config"]["model"] == "cheap"
        assert cfg["ocr"]["vlm_config"]["base_url"] == (
            "http://litellm:4000/v1"
        )
        # Key rides per-request: xberg 1.0.8 drops provider-env key
        # resolution when base_url is overridden (verified 2026-08-03).
        assert cfg["ocr"]["vlm_config"]["api_key"] == "sekret"
        assert len(files) == 2

    def test_vlm_model_env_override(self, fake_http, monkeypatch):
        monkeypatch.setenv("XBERG_VLM_API_KEY", "sekret")
        monkeypatch.setenv("XBERG_VLM_MODEL", "smart")
        fake_http(_xberg_response("X", "Y"))
        r = pdf_read.extract_pdf_text(SCANNED, ocr="llm")
        assert r["ocr_engine"] == "xberg:vlm:smart"

    def test_llm_is_all_pages_even_with_text_layer(
        self, fake_http, monkeypatch,
    ):
        """ocr='llm' skips native extraction entirely — a text PDF still
        goes through the model (caller explicitly asked for it)."""
        monkeypatch.setenv("XBERG_VLM_API_KEY", "sekret")
        fake_http(_xberg_response("A", "B"))
        r = pdf_read.extract_pdf_text(SAMPLE, ocr="llm")
        assert r["ocr_pages"] == [1, 2]
        assert "Invoice total" not in r["text"]


# ===================================================================
# Rendering + assembly
# ===================================================================


class TestRender:
    def test_render_page_bytes_jpeg(self):
        img, mime, w, h, pc = pdf_read.render_pdf_page_bytes(SCANNED, 1)
        assert mime == "image/jpeg"
        assert img[:3] == b"\xff\xd8\xff"  # JPEG magic
        assert w == 1200
        assert pc == 2

    def test_render_page_bytes_png(self):
        img, mime, *_ = pdf_read.render_pdf_page_bytes(
            SCANNED, 1, fmt="png",
        )
        assert mime == "image/png"
        assert img[:8] == b"\x89PNG\r\n\x1a\n"

    def test_render_out_of_range_raises_with_page_count(self):
        with pytest.raises(pdf_read.PageOutOfRangeError) as ei:
            pdf_read.render_pdf_page_bytes(SCANNED, 99)
        assert ei.value.page_count == 2


class TestAssemblePages:
    def test_under_ceiling_no_truncation(self):
        text, pages, truncated = pdf_read.assemble_pages(
            {1: "aaa", 2: "bbb"}, max_bytes=1000,
        )
        assert text == "aaa\fbbb"
        assert pages == [1, 2]
        assert truncated is False

    def test_ceiling_truncates_on_page_boundary(self):
        text, pages, truncated = pdf_read.assemble_pages(
            {1: "a" * 50, 2: "b" * 50, 3: "c" * 50}, max_bytes=80,
        )
        assert pages == [1]
        assert truncated is True
        assert "pages=2-3" in text

    def test_single_oversized_page_truncated_mid_text(self):
        text, pages, truncated = pdf_read.assemble_pages(
            {1: "x" * 500}, max_bytes=100,
        )
        assert pages == [1]
        assert truncated is True
        assert text.startswith("x" * 100)


# ===================================================================
# Server-layer: render_attachment_page returns an MCP Image
# ===================================================================


@pytest.fixture
def server_mod(tmp_token_db):
    """Import google_accounts_mcp.server with DB_PATH already pointed at a tmp file
    (module-level GmailClient() would otherwise open /data/tokens.db)."""
    from google_accounts_mcp import server

    return server


class TestServerRenderTool:
    def test_default_returns_image_content(self, server_mod, monkeypatch):
        server = server_mod
        from mcp.server.fastmcp.utilities.types import Image

        monkeypatch.setattr(
            server.client, "render_attachment_page_raw",
            MagicMock(return_value={
                "image_bytes": b"\xff\xd8\xffjpegbytes",
                "mime_type": "image/jpeg",
                "width": 10, "height": 10, "page": 1, "page_count": 1,
                "account": "a@b.c", "filename": "f.pdf",
            }),
        )
        out = server.render_attachment_page("m", "f.pdf", 1)
        assert isinstance(out, Image)
        content = out.to_image_content()
        assert content.mimeType == "image/jpeg"

    def test_error_returns_json_string(self, server_mod, monkeypatch):
        server = server_mod
        monkeypatch.setattr(
            server.client, "render_attachment_page_raw",
            MagicMock(return_value={"error": "no such attachment"}),
        )
        out = server.render_attachment_page("m", "f.pdf", 1)
        assert isinstance(out, str)
        assert json.loads(out)["error"] == "no such attachment"

    def test_return_base64_uses_legacy_json_path(self, server_mod, monkeypatch):
        server = server_mod
        monkeypatch.setattr(
            server.client, "render_attachment_page",
            MagicMock(return_value='{"image_base64": "QUJD"}'),
        )
        out = server.render_attachment_page(
            "m", "f.pdf", 1, return_base64=True,
        )
        assert isinstance(out, str)
        assert json.loads(out)["image_base64"] == "QUJD"
