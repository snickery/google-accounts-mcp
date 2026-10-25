"""Generate a tiny 2-page PDF fixture for google-accounts-mcp tests.

Runs via `uv run --with reportlab` so reportlab doesn't need to be a
project dep. Re-run after changing the text so the checked-in fixture
matches what the tests expect.
"""
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

out = Path(__file__).parent / "sample.pdf"
c = canvas.Canvas(str(out), pagesize=letter)

# Page 1 — plain text block
c.setFont("Helvetica", 12)
c.drawString(72, 720, "google-accounts-mcp fixture PDF")
c.drawString(72, 700, "Page 1 of 2")
c.drawString(72, 680, "Invoice total: $42.50")
c.drawString(72, 660, "Energy used: 123.4 kWh")
c.showPage()

# Page 2 — different text so we can assert page isolation
c.setFont("Helvetica", 12)
c.drawString(72, 720, "Page 2 of 2")
c.drawString(72, 700, "Thank you for your business.")
c.drawString(72, 680, "Reference: INV-00042")
c.showPage()

c.save()
print(f"Wrote {out} ({out.stat().st_size} bytes)")
