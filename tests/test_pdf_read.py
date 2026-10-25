"""Server layer of render_attachment_page (returns an MCP Image).

The PDF module itself (validate_options, OCR tiers, rendering) is tested in
mcp-server-kit (tests/test_pdf.py), where it now lives.
"""

import json
from unittest.mock import MagicMock

import pytest

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
