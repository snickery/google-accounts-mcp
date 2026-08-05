"""PDF reading helpers: text extraction, xberg OCR, page render.

This module is intentionally self-contained (stdlib + PDF deps only, no
imports from the surrounding package) because it is DUPLICATED VERBATIM in
the sibling notion-mcp repo (src/notion_mcp/pdf_read.py) — the two servers
need identical PDF semantics and a shared package would add a cross-repo
release dependency. Edit both copies in the same change.

Reading tiers
  A. Native text — pdfplumber (pdfminer.six) text / layout / tables.
  B. OCR        — pages rasterised at 2x via pypdfium2 and sent to an
     xberg server (https://github.com/xberg-io/xberg — POST /extract,
     markdown out). Default backend
     tesseract: xberg's paddle-ocr (the PP-OCR family the pre-2026-08
     in-process RapidOCR used) takes 4-5+ MINUTES on a dense text page
     on CPU in either model tier (measured 2026-08-03) vs ~3 s for
     tesseract — set XBERG_OCR_BACKEND=paddle-ocr only if you can eat
     that latency; use ocr='llm' for hard documents instead.
  C. VLM OCR    — same xberg call with the ``vlm`` backend: xberg forwards
     page images to an OpenAI-compatible vision endpoint (e.g. a
     LiteLLM gateway). The gateway key rides in the per-request vlm_config —
     xberg 1.0.8 cannot resolve provider env keys once base_url is
     overridden per request (verified 2026-08-03; revisit if upstream
     fixes env-key resolution, the server-side key was the preferred
     design). Best quality on handwriting / messy tables.

The ``ocr`` parameter of :func:`extract_pdf_text` selects the tier:
  'auto'  (default) — tier A, then tier B for requested pages whose native
          text came back < OCR_AUTO_MIN_CHARS non-whitespace chars.
  'off'   — tier A only.
  'force' — tier B on every requested page (skips tier A).
  'llm'   — tier C on every requested page (skips tier A).

Env: XBERG_BASE_URL (default http://xberg:8000 — point it at your xberg
server; if none is reachable, auto-tier OCR degrades to native text with
an ocr_error note), XBERG_TIMEOUT (s, default 120), XBERG_OCR_BACKEND (default 'tesseract'),
XBERG_VLM_MODEL (default 'cheap' — e.g. a LiteLLM gateway alias,
passed verbatim),
XBERG_VLM_BASE_URL (default http://litellm:4000/v1 — an
OpenAI-compatible endpoint, resolved by the xberg SERVER, not this
process), XBERG_VLM_API_KEY (required for ocr='llm' only — the
LiteLLM virtual key).
"""

import contextlib
import io
import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

VALID_MODES = ("text", "layout", "tables")
VALID_OCR = ("auto", "off", "force", "llm")

# Below this many non-whitespace chars of native text, a page is treated as
# image-only and OCR'd in 'auto' mode. Real text pages are hundreds of chars;
# scans typically yield 0 (or a few stray glyphs from a vector letterhead).
OCR_AUTO_MIN_CHARS = 20

# 72 dpi * 2 = 144 dpi input to the OCR engine — the PP-OCR sweet spot for
# ordinary print sizes; higher scales cost quadratic memory for no gain.
OCR_RENDER_SCALE = 2.0

XBERG_BASE_URL_DEFAULT = "http://xberg:8000"
XBERG_TIMEOUT_DEFAULT = 120.0
XBERG_OCR_BACKEND_DEFAULT = "tesseract"
XBERG_VLM_MODEL_DEFAULT = "cheap"
XBERG_VLM_BASE_URL_DEFAULT = "http://litellm:4000/v1"


def validate_options(mode: str, ocr: str) -> tuple[str, str]:
    """Normalise and validate the ``mode`` / ``ocr`` options, raising
    ValueError on anything unknown. Exposed separately so tool wrappers can
    fail fast on bad parameters BEFORE fetching the document bytes."""
    mode_norm = (mode or "text").strip().lower()
    if mode_norm not in VALID_MODES:
        raise ValueError(
            f"mode must be one of 'text', 'layout', 'tables' — got {mode!r}."
        )
    ocr_norm = (ocr or "auto").strip().lower()
    if ocr_norm not in VALID_OCR:
        raise ValueError(
            f"ocr must be one of 'auto', 'off', 'force', 'llm' — got {ocr!r}."
        )
    return mode_norm, ocr_norm


class PageOutOfRangeError(ValueError):
    """Requested page exceeds the document's page count."""

    def __init__(self, page: int, page_count: int):
        self.page = page
        self.page_count = page_count
        super().__init__(
            f"page {page} out of range (document has {page_count} pages)."
        )


def parse_page_spec(spec: str | None, page_count: int) -> list[int]:
    """Parse a ``pdftotext``-style page range string into a sorted list of
    1-indexed page numbers, clamped to ``1..page_count``.

    Accepts ``None`` / ``""`` (all pages), a single page (``"3"``), a range
    (``"1-5"``), a comma-separated list (``"1,3,5"``), or a mix
    (``"1-3,7,10-12"``). Whitespace is tolerated. Pages outside
    ``1..page_count`` are silently dropped — callers report the effective
    set via ``pages_returned`` in the response."""
    if spec is None or not str(spec).strip():
        return list(range(1, page_count + 1))
    wanted: set[int] = set()
    for chunk in str(spec).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo_s, _, hi_s = chunk.partition("-")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError as e:
                raise ValueError(
                    f"Invalid page range '{chunk}' in pages={spec!r}."
                ) from e
            if lo > hi:
                lo, hi = hi, lo
            for p in range(lo, hi + 1):
                if 1 <= p <= page_count:
                    wanted.add(p)
        else:
            try:
                p = int(chunk)
            except ValueError as e:
                raise ValueError(
                    f"Invalid page number '{chunk}' in pages={spec!r}."
                ) from e
            if 1 <= p <= page_count:
                wanted.add(p)
    return sorted(wanted)


# --- rendering --------------------------------------------------------------


def _null_or(concurrency: threading.Semaphore | None):
    return concurrency if concurrency is not None else contextlib.nullcontext()


def _render_page_pil(pdf, page_index: int, scale: float):
    """Render one pypdfium2 page to a PIL image with explicit native-memory
    cleanup. pypdfium2 holds PDFium buffers the GC reclaims only
    non-deterministically — close page and bitmap explicitly so a multi-page
    OCR sweep can't pile up native allocations. The returned PIL image owns
    a Python-side copy (``.convert()`` detaches from the bitmap buffer)."""
    page = pdf[page_index]
    try:
        bitmap = page.render(scale=scale)
        try:
            return bitmap.to_pil().convert("RGB")
        finally:
            bitmap.close()
    finally:
        page.close()


def render_pdf_page_bytes(
    file_bytes: bytes,
    page: int,
    max_width: int = 1200,
    fmt: str = "jpeg",
    quality: int = 75,
    concurrency: threading.Semaphore | None = None,
) -> tuple[bytes, str, int, int, int]:
    """Render one 1-indexed PDF page to encoded image bytes.

    Returns ``(image_bytes, mime_type, width, height, page_count)``.
    Raises :class:`PageOutOfRangeError` when ``page > page_count``."""
    import pypdfium2  # type: ignore
    from PIL import Image  # type: ignore

    buf = io.BytesIO()
    with _null_or(concurrency):
        pdf = pypdfium2.PdfDocument(io.BytesIO(file_bytes))
        try:
            page_count = len(pdf)
            if page > page_count:
                raise PageOutOfRangeError(page, page_count)
            pdf_page = pdf[page - 1]
            try:
                # pypdfium2 renders at 72 DPI * scale. Pick the scale that
                # makes the output width land near max_width.
                native_w, _native_h = pdf_page.get_size()
                if native_w <= 0:
                    native_w = 612.0  # US Letter fallback
                scale = max_width / float(native_w)
                if scale <= 0:
                    scale = 1.0
                bitmap = pdf_page.render(scale=scale)
                try:
                    pil = bitmap.to_pil()
                    # PDFium can overshoot max_width by a pixel — downscale
                    # rather than ship a larger image than asked.
                    if pil.width > max_width:
                        ratio = max_width / pil.width
                        pil = pil.resize(
                            (max_width, max(1, int(pil.height * ratio))),
                            Image.Resampling.LANCZOS,
                        )
                    if fmt == "jpeg":
                        # PDFium renders with alpha; JPEG can't hold one.
                        if pil.mode != "RGB":
                            pil = pil.convert("RGB")
                        pil.save(
                            buf, format="JPEG", quality=quality, optimize=True,
                        )
                        out_mime = "image/jpeg"
                    else:
                        pil.save(buf, format="PNG", optimize=True)
                        out_mime = "image/png"
                    out_w, out_h = pil.width, pil.height
                finally:
                    bitmap.close()
            finally:
                pdf_page.close()
        finally:
            pdf.close()
    return buf.getvalue(), out_mime, out_w, out_h, page_count


def _render_pages_png(
    file_bytes: bytes,
    page_numbers: list[int],
    scale: float,
    concurrency: threading.Semaphore | None,
) -> dict[int, bytes]:
    """Rasterise the given 1-indexed pages to PNG bytes at ``scale``."""
    import pypdfium2  # type: ignore

    out: dict[int, bytes] = {}
    with _null_or(concurrency):
        pdf = pypdfium2.PdfDocument(io.BytesIO(file_bytes))
        try:
            for p in page_numbers:
                pil = _render_page_pil(pdf, p - 1, scale)
                buf = io.BytesIO()
                pil.save(buf, format="PNG")
                out[p] = buf.getvalue()
        finally:
            pdf.close()
    return out


def _pdf_page_count(file_bytes: bytes) -> int:
    import pypdfium2  # type: ignore

    pdf = pypdfium2.PdfDocument(io.BytesIO(file_bytes))
    try:
        return len(pdf)
    finally:
        pdf.close()


# --- xberg OCR (tiers B and C) ----------------------------------------------


def _xberg_config() -> tuple[str, float]:
    base_url = (
        os.environ.get("XBERG_BASE_URL", "").strip().rstrip("/")
        or XBERG_BASE_URL_DEFAULT
    )
    timeout = float(
        os.environ.get("XBERG_TIMEOUT", "").strip() or XBERG_TIMEOUT_DEFAULT
    )
    return base_url, timeout


def xberg_ocr_pages(
    file_bytes: bytes,
    page_numbers: list[int],
    *,
    vlm: bool = False,
    concurrency: threading.Semaphore | None = None,
) -> tuple[dict[int, str], str]:
    """OCR the given 1-indexed pages via the configured xberg server.

    Pages are rasterised locally (PNG at OCR_RENDER_SCALE) and uploaded in
    ONE multipart request — xberg's ``results`` array maps 1:1 to the
    uploaded files, which keeps the per-page mapping trivial and sidesteps
    any question of how xberg splits a multi-page PDF. ``vlm=True`` selects
    xberg's vision-model backend (tier C); the provider key lives in
    xberg's own env, never in this process.

    Returns ``({page: markdown_text}, engine_label)``. Raises RuntimeError
    with xberg's status/body on failure so the caller sees exactly what
    the service said."""
    import httpx  # type: ignore

    base_url, timeout = _xberg_config()
    if vlm:
        model = (
            os.environ.get("XBERG_VLM_MODEL", "").strip()
            or XBERG_VLM_MODEL_DEFAULT
        )
        vlm_base = (
            os.environ.get("XBERG_VLM_BASE_URL", "").strip().rstrip("/")
            or XBERG_VLM_BASE_URL_DEFAULT
        )
        api_key = os.environ.get("XBERG_VLM_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "ocr='llm' requires XBERG_VLM_API_KEY in the server env "
                "(the LiteLLM virtual key; xberg 1.0.8 cannot resolve "
                "provider env keys when base_url is overridden per "
                "request). Use ocr='force' for classical OCR instead."
            )
        config = {
            "ocr": {
                "backend": "vlm",
                "vlm_config": {
                    "model": model,
                    "base_url": vlm_base,
                    "api_key": api_key,
                },
            }
        }
        engine = f"xberg:vlm:{model}"
    else:
        backend = (
            os.environ.get("XBERG_OCR_BACKEND", "").strip()
            or XBERG_OCR_BACKEND_DEFAULT
        )
        # Language codes are backend-specific: tesseract wants ISO 639-2
        # ("eng"), the paddle/candle family wants ISO 639-1 ("en").
        language = "eng" if backend == "tesseract" else "en"
        config = {"ocr": {"backend": backend, "language": language}}
        engine = f"xberg:{backend}"

    rendered = _render_pages_png(
        file_bytes, page_numbers, OCR_RENDER_SCALE, concurrency,
    )
    ordered = sorted(rendered.keys())
    files = [
        ("files", (f"page-{p}.png", rendered[p], "image/png"))
        for p in ordered
    ]
    data = {"output_format": "markdown", "config": json.dumps(config)}

    with httpx.Client(timeout=timeout) as http:
        resp = http.post(f"{base_url}/extract", data=data, files=files)
    if resp.status_code != 200:
        raise RuntimeError(
            f"xberg OCR request for pages {ordered} failed: HTTP "
            f"{resp.status_code} from {base_url}: {resp.text[:500]}"
        )
    try:
        payload = resp.json()
        errors = payload.get("summary", {}).get("errors", 0)
        results = payload["results"]
    except (KeyError, ValueError) as e:
        raise RuntimeError(
            f"xberg OCR response had an unexpected shape: {resp.text[:500]}"
        ) from e
    if errors:
        raise RuntimeError(
            f"xberg reported {errors} extraction error(s) for pages "
            f"{ordered}: {resp.text[:500]}"
        )
    if len(results) != len(ordered):
        raise RuntimeError(
            f"xberg returned {len(results)} results for {len(ordered)} "
            f"uploaded pages — refusing to guess the page mapping."
        )
    out = {
        p: (results[i].get("content") or "").strip()
        for i, p in enumerate(ordered)
    }
    return out, engine


# --- assembly ----------------------------------------------------------------


def assemble_pages(
    per_page_text: dict[int, str], max_bytes: int,
) -> tuple[str, list[int], bool]:
    """Join per-page text under a byte ceiling. Walk pages in order and stop
    as soon as appending the next page would exceed the ceiling. If the
    first page on its own is already over the ceiling, include it but
    truncate mid-text so the caller at least gets something.

    Returns ``(text, pages_included, truncated)`` — the truncation marker
    telling the caller which pages to fetch next is appended to ``text``."""
    targets_in_order = sorted(per_page_text.keys())
    pieces: list[str] = []
    pages_included: list[int] = []
    running_bytes = 0
    truncated = False
    truncation_note: str | None = None
    for p in targets_in_order:
        body = per_page_text[p]
        chunk = ("\f" if pieces else "") + body
        encoded = chunk.encode("utf-8", errors="replace")
        if running_bytes + len(encoded) > max_bytes:
            included_p = False
            if not pieces:
                # Single page is already too big — truncate it.
                slack = max_bytes - running_bytes
                body_enc = body.encode("utf-8", errors="replace")[:slack]
                pieces.append(body_enc.decode("utf-8", errors="replace"))
                pages_included.append(p)
                running_bytes += len(body_enc)
                included_p = True
            truncated = True
            # The overflowing page p was dropped unless it was force-included
            # above — the retry hint must cover it in that case (the original
            # original implementation skipped it: off-by-one).
            remaining = [
                q for q in targets_in_order
                if (q > p if included_p else q >= p)
            ]
            if remaining:
                lo, hi = remaining[0], remaining[-1]
                truncation_note = (
                    f"...[truncated at page {pages_included[-1]}, "
                    f"call again with pages={lo}-{hi}]"
                )
            else:
                truncation_note = (
                    f"...[truncated mid-page {pages_included[-1]}, "
                    f"call again with pages={pages_included[-1]} and "
                    f"a narrower mode]"
                )
            break
        pieces.append(chunk)
        pages_included.append(p)
        running_bytes += len(encoded)

    text_out = "".join(pieces)
    if truncation_note:
        text_out += "\n" + truncation_note
    return text_out, pages_included, truncated


# --- top-level extraction ------------------------------------------------------


def extract_pdf_text(
    file_bytes: bytes,
    pages: str | None = None,
    mode: str = "text",
    ocr: str = "auto",
    *,
    max_bytes: int = 200_000,
    concurrency: threading.Semaphore | None = None,
) -> dict:
    """Extract text from PDF bytes. Returns a dict with keys ``text``,
    ``page_count``, ``pages_returned``, ``mode``, ``truncated``, plus
    ``ocr_used`` / ``ocr_pages`` / ``ocr_engine`` when OCR ran (and
    ``ocr_error`` when 'auto'-mode OCR was needed but failed — the caller
    then still gets the native-text result instead of an exception).

    ``mode`` applies to native extraction only; OCR'd pages come back as
    markdown-flavoured text from xberg (both tiers)."""
    mode_norm, ocr_norm = validate_options(mode, ocr)

    per_page_text: dict[int, str] = {}

    if ocr_norm in ("force", "llm"):
        # Native extraction would be discarded — only the page count is
        # needed to resolve the page spec.
        page_count = _pdf_page_count(file_bytes)
        targets = parse_page_spec(pages, page_count)
        per_page_text = {p: "" for p in targets}
    else:
        import pdfplumber  # type: ignore

        with _null_or(concurrency), pdfplumber.open(
            io.BytesIO(file_bytes)
        ) as pdf:
            page_count = len(pdf.pages)
            targets = parse_page_spec(pages, page_count)
            for p in targets:
                page = pdf.pages[p - 1]
                if mode_norm == "text":
                    per_page_text[p] = page.extract_text() or ""
                elif mode_norm == "layout":
                    per_page_text[p] = page.extract_text(layout=True) or ""
                else:  # tables
                    tables = page.extract_tables() or []
                    if not tables:
                        per_page_text[p] = ""
                        continue
                    parts: list[str] = []
                    for ti, table in enumerate(tables, 1):
                        parts.append(f"## Page {p} Table {ti}")
                        for row in table:
                            cells = [
                                ("" if c is None else str(c)).replace(
                                    "|", "\\|"
                                )
                                for c in row
                            ]
                            parts.append("| " + " | ".join(cells) + " |")
                        parts.append("")
                    per_page_text[p] = "\n".join(parts)

    # Pick the pages OCR should cover.
    if ocr_norm == "off":
        ocr_targets: list[int] = []
    elif ocr_norm == "auto":
        ocr_targets = [
            p
            for p in sorted(per_page_text.keys())
            if len(per_page_text[p].strip()) < OCR_AUTO_MIN_CHARS
        ]
    else:  # force / llm
        ocr_targets = sorted(per_page_text.keys())

    ocr_used = False
    ocr_engine: str | None = None
    ocr_error: str | None = None
    if ocr_targets:
        try:
            ocr_text, ocr_engine = xberg_ocr_pages(
                file_bytes,
                ocr_targets,
                vlm=(ocr_norm == "llm"),
                concurrency=concurrency,
            )
            per_page_text.update(ocr_text)
            ocr_used = True
        except Exception as e:
            # 'auto' is best-effort: a broken OCR stack must not take down
            # plain text extraction — surface the failure in the result
            # instead (the silent-failure trap this module replaces).
            # An explicit 'force'/'llm' request propagates.
            if ocr_norm != "auto":
                raise
            logger.warning("auto-mode OCR failed: %s", e)
            ocr_error = f"{type(e).__name__}: {e}"

    text_out, pages_included, truncated = assemble_pages(
        per_page_text, max_bytes,
    )

    result = {
        "text": text_out,
        "page_count": page_count,
        "pages_returned": pages_included,
        "mode": mode_norm,
        "truncated": truncated,
    }
    if ocr_used:
        result["ocr_used"] = True
        result["ocr_pages"] = ocr_targets
        result["ocr_engine"] = ocr_engine
    if ocr_error is not None:
        result["ocr_error"] = ocr_error
    return result
