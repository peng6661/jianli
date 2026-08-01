#!/usr/bin/env python3
"""
Resume Editor - PDF Export Service (Cloud Edition)

Architecture: localStorage cloud deployment = static site + lightweight backend
- Uses WeasyPrint for HTML-to-PDF conversion (pure Python, no browser needed)
- WeasyPrint renders images synchronously, eliminating async loading issues
- Memory footprint ~50MB (vs 300-500MB for Edge/Chromium)
- Two-pass render: measures content height via box tree traversal, then
  generates a single-page PDF with exact height (default view only)
- No Git API, no database, stateless
- CORS configurable via environment variable
"""

import os
import re
import sys
import math
import logging
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

# WeasyPrint
try:
    from weasyprint import HTML, __version__ as WEASYPRINT_VERSION
    WEASYPRINT_AVAILABLE = True
except ImportError:
    try:
        import weasyprint
        from weasyprint import HTML
        WEASYPRINT_VERSION = getattr(weasyprint, "__version__", "unknown")
        WEASYPRINT_AVAILABLE = True
    except ImportError:
        WEASYPRINT_AVAILABLE = False
        WEASYPRINT_VERSION = None

# ============================================
# Logging
# ============================================
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("resume-pdf")

# ============================================
# FastAPI App
# ============================================
app = FastAPI(
    title="Resume Editor PDF Service",
    description="WeasyPrint HTML-to-PDF conversion (cloud edition)",
    version="5.0.0",
)

# CORS - configurable via ALLOWED_ORIGINS env var (comma-separated)
_allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
_allowed_origins = [o.strip() for o in _allowed_origins if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================
# Request / Response Models
# ============================================
class PdfExportRequest(BaseModel):
    html: str
    filename: str = "Resume Editor"


class HealthResponse(BaseModel):
    status: str
    engine: str
    engine_version: Optional[str] = None


# ============================================
# PDF Export Core
# ============================================

# CSS injected into every PDF to fix Docker-specific rendering issues.
# 1. Font override: --ant-font uses macOS/Windows fonts that don't exist
#    in the Docker container. Replace with Liberation Sans (Arial-compatible)
#    + WenQuanYi Zen Hei (CJK).
# 2. Remove preview-only visual styles (border, shadow, rounded corners)
#    that should not appear in the exported PDF.
# 3. Ensure colors print correctly (WeasyPrint needs print-color-adjust).
_PDF_OVERRIDE_CSS = """
:root {
    --ant-font: 'Liberation Sans', 'WenQuanYi Zen Hei', 'DejaVu Sans', sans-serif !important;
}
body, .resume-paper, .paginated-page {
    font-family: 'Liberation Sans', 'WenQuanYi Zen Hei', 'DejaVu Sans', sans-serif !important;
}
.resume-paper {
    border: none !important;
    box-shadow: none !important;
    border-radius: 0 !important;
}
body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
"""


def _remove_scripts(html: str) -> str:
    """Remove all <script> tags. WeasyPrint does not execute JavaScript,
    so any embedded scripts (e.g. the frontend's re-measurement script)
    are dead code that only wastes bandwidth."""
    return re.sub(
        r"<script[^>]*>.*?</script>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )


def _inject_css(html: str, css: str) -> str:
    """Inject a <style> block at the end of <head>.
    Because CSS cascade rules resolve conflicts by order, styles injected
    here override all preceding stylesheets."""
    style_tag = f"<style>{css}</style>"
    if "</head>" in html:
        return html.replace("</head>", f"{style_tag}</head>", 1)
    return style_tag + html


def _is_paginated_view(html: str) -> bool:
    """Check if the HTML uses A4 page size (paginated view).
    The frontend sets @page { size: A4 } for paginated view and
    @page { size: 210mm <N>mm } for default (single-page) view."""
    return bool(re.search(r"@page\s*\{[^}]*size:\s*A4", html, re.IGNORECASE))


def _measure_content_height_mm(html: str) -> int:
    """Render HTML with a very tall page, then traverse WeasyPrint's
    layout box tree to find the exact bottom position of all content.

    This replaces the frontend's browser-based measurement
    (getBoundingClientRect) which gives incorrect results because
    WeasyPrint renders fonts and spacing differently from the browser.

    Returns: content height in millimeters (with a small buffer).
    """
    # Inject a very tall page so all content lands on a single page
    measure_html = _inject_css(
        html, "@page { size: 210mm 100000mm; margin: 0; }"
    )

    try:
        doc = HTML(string=measure_html).render()
    except Exception as e:
        log.warning("[measure] Render failed: %s, using A4 fallback", e)
        return 297

    if not doc.pages:
        return 297

    # Traverse the box tree to find the maximum content bottom position
    max_bottom_px = 0

    def visit_box(box):
        nonlocal max_bottom_px
        y = getattr(box, "position_y", 0) or 0
        h = getattr(box, "height", 0) or 0
        # margin_height() includes margins, which is more accurate
        try:
            mh = box.margin_height()
            if mh is not None and mh > h:
                h = mh
        except (AttributeError, TypeError):
            pass
        bottom = y + h
        if bottom > max_bottom_px:
            max_bottom_px = bottom
        for child in getattr(box, "children", []):
            visit_box(child)

    for page in doc.pages:
        # WeasyPrint stores the root box under _page_box (internal API)
        pb = getattr(page, "_page_box", None)
        if pb is None:
            pb = getattr(page, "page_box", None)
        if pb is not None:
            visit_box(pb)

    if max_bottom_px > 0:
        # 96 DPI: 1mm = 3.7795 px
        height_mm = math.ceil(max_bottom_px / 3.7795) + 2  # 2mm buffer
        log.info(
            "[measure] Content height: %dpx -> %dmm", max_bottom_px, height_mm
        )
        return height_mm

    # Fallback: render with A4 and use page count
    log.warning("[measure] Box tree traversal returned 0, using A4 fallback")
    a4_html = _inject_css(html, "@page { size: 210mm 297mm; margin: 0; }")
    try:
        a4_doc = HTML(string=a4_html).render()
        num_pages = len(a4_doc.pages)
        height_mm = num_pages * 297
        log.info("[measure] Fallback: %d A4 pages -> %dmm", num_pages, height_mm)
        return height_mm
    except Exception:
        return 297


def convert_html_to_pdf(html: str, filename: str) -> bytes:
    """Convert HTML to PDF using WeasyPrint with two-pass rendering.

    For default (single-page) view:
      Pass 1 - Render with a very tall page and traverse the layout box
               tree to measure the exact content height.
      Pass 2 - Re-render with @page { size: 210mm <measured>mm } to
               produce a single-page PDF whose height matches the content.

    For paginated view:
      Render directly with A4 page size (WeasyPrint handles page breaks).

    In both cases, the HTML is preprocessed to:
      - Remove <script> tags (WeasyPrint does not execute JavaScript)
      - Inject CSS overrides for Docker font compatibility and visual cleanup
    """
    if not WEASYPRINT_AVAILABLE:
        raise RuntimeError(
            "WeasyPrint is not installed. Run: pip install weasyprint"
        )

    try:
        log.info("Generating PDF via WeasyPrint: %s", filename)
        log.info("HTML length: %d chars", len(html))

        # 1. Remove scripts (WeasyPrint ignores JavaScript)
        html_clean = _remove_scripts(html)

        # 2. Inject font + visual overrides
        html_styled = _inject_css(html_clean, _PDF_OVERRIDE_CSS)

        # 3. Determine view mode and render
        is_paginated = _is_paginated_view(html_styled)

        if is_paginated:
            # Paginated view: A4 multi-page, WeasyPrint handles breaks
            log.info("[pdf] Paginated view: rendering with A4 page size")
            pdf_bytes = HTML(string=html_styled).write_pdf()
        else:
            # Default view: single page with content-determined height
            height_mm = _measure_content_height_mm(html_styled)
            log.info("[pdf] Default view: single page, height=%dmm", height_mm)
            final_html = _inject_css(
                html_styled,
                f"@page {{ size: 210mm {height_mm}mm; margin: 0; }}",
            )
            pdf_bytes = HTML(string=final_html).write_pdf()

        if not pdf_bytes or len(pdf_bytes) == 0:
            raise RuntimeError("WeasyPrint produced empty PDF")

        log.info("PDF generated: %s (%d bytes)", filename, len(pdf_bytes))
        return pdf_bytes

    except Exception as e:
        log.error("PDF generation failed: %s", str(e), exc_info=True)
        raise RuntimeError(f"PDF generation failed: {str(e)}") from e


# ============================================
# API Endpoints
# ============================================
@app.get("/api/health", response_model=HealthResponse)
def health_check():
    """Health check - verify PDF engine is available."""
    return HealthResponse(
        status="ok" if WEASYPRINT_AVAILABLE else "degraded",
        engine="weasyprint" if WEASYPRINT_AVAILABLE else "none",
        engine_version=WEASYPRINT_VERSION,
    )


@app.get("/api/test-pdf")
def test_pdf():
    """Minimal PDF test - generate a simple PDF to verify WeasyPrint works."""
    if not WEASYPRINT_AVAILABLE:
        return {"status": "failed", "error": "WeasyPrint not installed"}

    test_html = (
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<style>@page { size: A4; margin: 2cm; }'
        "body { font-family: sans-serif; font-size: 24px; }</style>"
        "</head><body><h1>WeasyPrint Test</h1>"
        "<p>Hello from Docker</p>"
        '<p style="color: #1677ff;">Color test</p>'
        '<div style="display: flex; justify-content: space-between;">'
        "<span>Left</span><span>Right</span></div>"
        "</body></html>"
    )

    try:
        pdf_bytes = HTML(string=test_html).write_pdf()
        return {
            "status": "ok" if pdf_bytes and len(pdf_bytes) > 0 else "failed",
            "engine": "weasyprint",
            "version": WEASYPRINT_VERSION,
            "pdf_size": len(pdf_bytes) if pdf_bytes else 0,
        }
    except Exception as e:
        log.error("[test-pdf] Exception: %s", str(e), exc_info=True)
        return {"status": "error", "message": str(e)}


# Keep old endpoint name as alias for backward compatibility
@app.get("/api/test-edge")
def test_edge_alias():
    """Alias for /api/test-pdf (backward compatibility)."""
    return test_pdf()


@app.post("/api/export-pdf")
async def export_pdf(request: PdfExportRequest):
    """Convert HTML to PDF and return as download.

    Request body:
        html: Full HTML document string
        filename: Export filename (optional)

    Returns: PDF file download
    """
    html = request.html
    filename = request.filename

    if not html or not html.strip():
        raise HTTPException(status_code=400, detail="HTML content cannot be empty")

    if not WEASYPRINT_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="PDF export service not ready (WeasyPrint not installed)",
        )

    try:
        pdf_bytes = convert_html_to_pdf(html, filename)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # RFC 5987 encoded filename for non-ASCII
    encoded_filename = filename.encode("utf-8")
    percent_encoded = "".join(f"%{b:02X}" for b in encoded_filename)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="resume.pdf"; '
                f"filename*=UTF-8''{percent_encoded}.pdf"
            ),
            "Content-Length": str(len(pdf_bytes)),
        },
    )


# ============================================
# Main
# ============================================
if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    host = os.environ.get("HOST", "0.0.0.0")

    print("=" * 50)
    print("  Resume Editor PDF Service (Cloud Edition)")
    print("=" * 50)
    print()
    print(f"  API:      http://{host}:{port}")
    print(f"  Health:   http://{host}:{port}/api/health")
    print(f"  Export:   POST http://{host}:{port}/api/export-pdf")
    print(f"  Test:     GET http://{host}:{port}/api/test-pdf")
    print(f"  CORS:     {_allowed_origins}")
    print()
    if WEASYPRINT_AVAILABLE:
        print(f"  PDF Engine: WeasyPrint v{WEASYPRINT_VERSION}")
    else:
        print("  WARNING: WeasyPrint not found, PDF export unavailable")
    print()
    print("  Press Ctrl+C to stop")
    print()

    uvicorn.run(app, host=host, port=port, log_level="info")
