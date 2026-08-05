"""Generate OCR-test PDF fixtures for gmail-mcp tests.

Two fixtures:
  scanned.pdf — 2 pages, IMAGE-ONLY (rendered text baked into a bitmap,
    zero extractable text). Exercises the OCR tiers.
  mixed.pdf   — page 1 real text, page 2 image-only. Exercises the
    'auto' per-page trigger: page 1 must NOT be OCR'd, page 2 must be.

Text is drawn large and high-contrast so the PP-OCR mobile models read it
reliably; assertions key on the digit strings (digits survive OCR best).

Run: uv run --with reportlab --with pillow --no-project make_ocr_fixtures.py
Re-run after changing the text so the checked-in fixtures match the tests.
"""
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

HERE = Path(__file__).parent


def text_image(lines: list[str]) -> Image.Image:
    """Render text lines onto a white 1224x1584 bitmap (letter @ ~144dpi)."""
    img = Image.new("RGB", (1224, 1584), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=56)
    y = 120
    for line in lines:
        d.text((100, y), line, fill="black", font=font)
        y += 110
    return img


def draw_image_page(c: canvas.Canvas, img: Image.Image) -> None:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    w, h = letter
    c.drawImage(ImageReader(buf), 0, 0, width=w, height=h)
    c.showPage()


# --- scanned.pdf: both pages image-only ---
out = HERE / "scanned.pdf"
c = canvas.Canvas(str(out), pagesize=letter)
draw_image_page(c, text_image([
    "SCANNED INVOICE",
    "Total due: 421.50",
    "Account: 88123",
]))
draw_image_page(c, text_image([
    "PAGE TWO",
    "Reference: OCR-7734",
]))
c.save()
print(f"Wrote {out} ({out.stat().st_size} bytes)")

# --- mixed.pdf: page 1 text, page 2 image-only ---
out = HERE / "mixed.pdf"
c = canvas.Canvas(str(out), pagesize=letter)
c.setFont("Helvetica", 12)
c.drawString(72, 720, "mixed fixture page 1 — native text layer")
c.drawString(72, 700, "Native total: $10.00")
c.showPage()
draw_image_page(c, text_image([
    "IMAGE ONLY PAGE",
    "Scan code: 55901",
]))
c.save()
print(f"Wrote {out} ({out.stat().st_size} bytes)")
