"""Tier 1 unit tests — pure functions, no API, no mocking needed.

Covers the helper methods on GmailClient that transform data:
  _strip_html, _extract_body, _find_attachments, _build_message,
  _encode_message, _resolve_attachments (path-traversal guard).
"""

import base64
from email.message import EmailMessage
from email.mime.text import MIMEText

import pytest

from google_accounts_mcp.gmail_client import GmailClient


@pytest.fixture
def bare_client():
    """A GmailClient instance used only for helper-method access. No DB init."""
    c = GmailClient.__new__(GmailClient)
    return c


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


# ===================================================================
# _strip_html
# ===================================================================

class TestStripHtml:
    def test_strips_tags(self, bare_client):
        assert bare_client._strip_html("<p>Hello</p>") == "Hello"

    def test_br_becomes_newline(self, bare_client):
        assert "\n" in bare_client._strip_html("line1<br>line2")

    def test_br_self_closing(self, bare_client):
        assert "\n" in bare_client._strip_html("line1<br/>line2")

    def test_paragraphs_separated(self, bare_client):
        out = bare_client._strip_html("<p>A</p><p>B</p>")
        assert "A" in out and "B" in out

    def test_html_entities_decoded(self, bare_client):
        assert bare_client._strip_html("&amp; &lt; &gt;") == "& < >"

    def test_collapses_blank_lines(self, bare_client):
        out = bare_client._strip_html("A<br><br><br><br>B")
        assert "\n\n\n" not in out

    def test_case_insensitive_tags(self, bare_client):
        assert "Hello" in bare_client._strip_html("<P>Hello</P>")


# ===================================================================
# _extract_body
# ===================================================================

class TestExtractBody:
    def test_text_plain_direct(self, bare_client):
        payload = {
            "mimeType": "text/plain",
            "body": {"data": _b64("plain text")},
        }
        assert bare_client._extract_body(payload) == "plain text"

    def test_text_html_direct(self, bare_client):
        payload = {
            "mimeType": "text/html",
            "body": {"data": _b64("<p>html text</p>")},
        }
        assert bare_client._extract_body(payload) == "html text"

    def test_multipart_prefers_plain(self, bare_client):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("plain wins")}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>html loses</p>")}},
            ],
        }
        assert bare_client._extract_body(payload) == "plain wins"

    def test_multipart_falls_back_to_html(self, bare_client):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64("<p>only html</p>")}},
            ],
        }
        assert bare_client._extract_body(payload) == "only html"

    def test_nested_multipart(self, bare_client):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _b64("nested plain")}},
                    ],
                },
            ],
        }
        assert bare_client._extract_body(payload) == "nested plain"

    def test_empty_payload(self, bare_client):
        assert bare_client._extract_body({}) == ""

    def test_attachments_only_lists_them(self, bare_client):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"filename": "doc.pdf", "mimeType": "application/pdf", "body": {}},
            ],
        }
        out = bare_client._extract_body(payload)
        assert "doc.pdf" in out
        assert "application/pdf" in out


# ===================================================================
# _find_attachments
# ===================================================================

class TestFindAttachments:
    def test_no_attachments(self, bare_client):
        payload = {"mimeType": "text/plain", "body": {"data": _b64("x")}}
        assert bare_client._find_attachments(payload) == []

    def test_single_attachment(self, bare_client):
        payload = {
            "parts": [
                {"filename": "report.pdf", "mimeType": "application/pdf", "body": {}},
            ],
        }
        found = bare_client._find_attachments(payload)
        assert len(found) == 1
        assert found[0]["filename"] == "report.pdf"

    def test_nested_attachments(self, bare_client):
        payload = {
            "parts": [
                {
                    "parts": [
                        {"filename": "a.txt", "body": {}},
                        {"filename": "b.txt", "body": {}},
                    ],
                },
            ],
        }
        found = bare_client._find_attachments(payload)
        assert [a["filename"] for a in found] == ["a.txt", "b.txt"]

    def test_ignores_parts_without_filename(self, bare_client):
        payload = {
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("x")}},
                {"filename": "x.zip", "body": {}},
            ],
        }
        found = bare_client._find_attachments(payload)
        assert len(found) == 1


# ===================================================================
# _part_disposition
# ===================================================================

class TestPartDisposition:
    def test_explicit_attachment(self, bare_client):
        part = {"filename": "x.pdf", "headers": [
            {"name": "Content-Disposition", "value": "attachment; filename=x.pdf"},
        ]}
        assert bare_client._part_disposition(part) == "attachment"

    def test_explicit_inline(self, bare_client):
        part = {"filename": "logo.png", "headers": [
            {"name": "Content-Disposition", "value": "inline"},
        ]}
        assert bare_client._part_disposition(part) == "inline"

    def test_content_id_implies_inline(self, bare_client):
        part = {"filename": "pic.png", "headers": [
            {"name": "Content-ID", "value": "<pic@abc>"},
        ]}
        assert bare_client._part_disposition(part) == "inline"

    def test_case_insensitive_header_names(self, bare_client):
        part = {"filename": "x.png", "headers": [
            {"name": "content-disposition", "value": "INLINE"},
        ]}
        assert bare_client._part_disposition(part) == "inline"

    def test_defaults_to_attachment_when_no_hints(self, bare_client):
        part = {"filename": "unknown.bin", "headers": []}
        assert bare_client._part_disposition(part) == "attachment"

    def test_missing_headers_key(self, bare_client):
        assert bare_client._part_disposition({"filename": "x"}) == "attachment"


# ===================================================================
# _build_message
# ===================================================================

class TestBuildMessage:
    def test_plain_text_no_attachments(self, bare_client):
        msg = bare_client._build_message(
            to=["a@b.com"], subject="hi", body="body text"
        )
        assert isinstance(msg, MIMEText)
        assert msg["To"] == "a@b.com"
        assert msg["Subject"] == "hi"

    def test_multiple_to(self, bare_client):
        msg = bare_client._build_message(
            to=["a@b.com", "c@d.com"], subject="hi", body="b"
        )
        assert msg["To"] == "a@b.com, c@d.com"

    def test_cc_and_bcc_lists(self, bare_client):
        msg = bare_client._build_message(
            to=["a@b.com"], subject="hi", body="b",
            cc=["cc1@b.com", "cc2@b.com"], bcc=["bcc@b.com"],
        )
        assert msg["Cc"] == "cc1@b.com, cc2@b.com"
        assert msg["Bcc"] == "bcc@b.com"

    def test_empty_cc_bcc(self, bare_client):
        msg = bare_client._build_message(
            to=["a@b.com"], subject="hi", body="b"
        )
        assert msg["Cc"] is None
        assert msg["Bcc"] is None

    def test_with_attachments_is_email_message(self, bare_client):
        msg = bare_client._build_message(
            to=["a@b.com"], subject="hi", body="b",
            attachments=[("file.txt", b"data-bytes")],
        )
        assert isinstance(msg, EmailMessage)
        # The attachment should appear among the iter_attachments output.
        attachments = list(msg.iter_attachments())
        assert len(attachments) == 1
        assert attachments[0].get_filename() == "file.txt"

    def test_attachment_mime_guess(self, bare_client):
        msg = bare_client._build_message(
            to=["a@b.com"], subject="hi", body="b",
            attachments=[("x.pdf", b"%PDF-1.4")],
        )
        att = next(iter(msg.iter_attachments()))
        assert att.get_content_type() == "application/pdf"

    def test_unknown_extension_falls_back_to_octet_stream(self, bare_client):
        msg = bare_client._build_message(
            to=["a@b.com"], subject="hi", body="b",
            attachments=[("weird.zzz", b"\x00\x01")],
        )
        att = next(iter(msg.iter_attachments()))
        assert att.get_content_type() == "application/octet-stream"


# ===================================================================
# _encode_message
# ===================================================================

class TestEncodeMessage:
    def test_returns_urlsafe_base64(self, bare_client):
        msg = MIMEText("hello")
        msg["To"] = "a@b.com"
        msg["Subject"] = "test"
        encoded = bare_client._encode_message(msg)
        # urlsafe base64 uses - and _ instead of + and /
        assert "+" not in encoded
        assert "/" not in encoded
        # Round-trip
        decoded = base64.urlsafe_b64decode(encoded + "===")
        assert b"a@b.com" in decoded
        assert b"hello" in decoded


# ===================================================================
# _resolve_local_attachment (path traversal guard, single path)
# ===================================================================

class TestResolveLocalAttachment:
    def test_relative_path_under_root(self, bare_client, tmp_attachments_dir):
        f = tmp_attachments_dir / "uploads" / "report.pdf"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"pdf-bytes")
        out = bare_client._resolve_local_attachment("uploads/report.pdf")
        assert out == f.resolve()

    def test_absolute_path_inside_root(self, bare_client, tmp_attachments_dir):
        f = tmp_attachments_dir / "data.bin"
        f.write_bytes(b"bytes")
        out = bare_client._resolve_local_attachment(str(f))
        assert out == f.resolve()

    def test_path_traversal_rejected(self, bare_client, tmp_attachments_dir):
        with pytest.raises(ValueError, match="outside ATTACHMENTS_DIR"):
            bare_client._resolve_local_attachment("../../../etc/passwd")

    def test_absolute_outside_root_rejected(self, bare_client, tmp_attachments_dir):
        with pytest.raises(ValueError, match="outside ATTACHMENTS_DIR"):
            bare_client._resolve_local_attachment("/etc/passwd")

    def test_missing_file_rejected(self, bare_client, tmp_attachments_dir):
        with pytest.raises(ValueError, match="not found"):
            bare_client._resolve_local_attachment("nonexistent.pdf")


# ===================================================================
# _parse_attachment_source — unified scheme parser
# ===================================================================

class TestParseAttachmentSource:
    def test_local_scheme(self, bare_client, tmp_attachments_dir):
        f = tmp_attachments_dir / "uploads" / "report.pdf"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"pdf-bytes")
        name, data = bare_client._parse_attachment_source("local:uploads/report.pdf")
        assert name == "report.pdf"
        assert data == b"pdf-bytes"

    def test_local_scheme_case_insensitive(self, bare_client, tmp_attachments_dir):
        f = tmp_attachments_dir / "x.txt"
        f.write_bytes(b"x")
        name, _ = bare_client._parse_attachment_source("LOCAL:x.txt")
        assert name == "x.txt"

    def test_empty_source_rejected(self, bare_client):
        with pytest.raises(ValueError, match="missing a scheme"):
            bare_client._parse_attachment_source("")

    def test_no_scheme_rejected(self, bare_client):
        with pytest.raises(ValueError, match="missing a scheme"):
            bare_client._parse_attachment_source("just-a-path.pdf")

    def test_unknown_scheme_rejected(self, bare_client):
        with pytest.raises(ValueError, match="Unknown attachment scheme 'http'"):
            bare_client._parse_attachment_source("http://example.com/foo.pdf")

    def test_base64_scheme_rejected(self, bare_client):
        # base64: is explicitly not supported — upload_file is the staging path.
        with pytest.raises(ValueError, match="Unknown attachment scheme 'base64'"):
            bare_client._parse_attachment_source("base64:SGVsbG8=")

    def test_empty_value_rejected(self, bare_client):
        with pytest.raises(ValueError, match="no value after the scheme"):
            bare_client._parse_attachment_source("local:")

    def test_path_traversal_bubbles_up(self, bare_client, tmp_attachments_dir):
        with pytest.raises(ValueError, match="outside ATTACHMENTS_DIR"):
            bare_client._parse_attachment_source("local:../../../etc/passwd")
