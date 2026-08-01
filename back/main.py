#!/usr/bin/env python3
"""
Resume Editor - PDF Export Service (Cloud Edition)

Architecture: localStorage cloud deployment = static site + lightweight backend
- Uses WeasyPrint for HTML-to-PDF conversion (pure Python, no browser needed)
- WeasyPrint renders images synchronously, eliminating async loading issues
  that required Edge's --virtual-time-budget
- Memory footprint ~50MB (vs 300-500MB for Edge/Chromium)
- Supports @page rules, base64 images, CSS variables, flexbox (v60+)
- No Git API, no database, stateless
- CORS configurable via environment variable
"""

import os
import sys
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
    version="4.0.0",
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
def convert_html_to_pdf(html: str, filename: str) -> bytes:
    """
    Convert HTML to PDF using WeasyPrint.

    WeasyPrint is a pure-Python HTML/CSS rendering engine that produces
    PDF directly without a browser. Key advantages over Edge headless:

    1. Synchronous image rendering: base64 images are decoded during
       layout, not asynchronously. No need for --virtual-time-budget.
    2. Low memory: ~50MB vs 300-500MB for Chromium-based browsers.
    3. Excellent @page support: designed for paged media, handles
       custom page sizes (210mm x Hh) and page breaks natively.
    4. No external process: runs in-process, no subprocess management,
       no D-Bus, no Xvfb, no shared memory issues.

    The @page CSS rules embedded in the HTML control page size:
    - Default view: @page{size:210mm <H>mm} (single page, height = content)
    - Paginated view: @page{size:A4} (standard multi-page A4)
    """
    if not WEASYPRINT_AVAILABLE:
        raise RuntimeError(
            "WeasyPrint is not installed. Run: pip install weasyprint"
        )

    try:
        log.info("Generating PDF via WeasyPrint: %s", filename)
        log.info("HTML length: %d chars", len(html))

        # WeasyPrint renders HTML string to PDF bytes directly.
        # The HTML contains all CSS inline (including @page rules),
        # all images as base64 data URIs, so no external resources needed.
        pdf_bytes = HTML(string=html).write_pdf()

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
    """
    Minimal PDF test - generate a simple PDF to verify WeasyPrint works.
    Replaces the old /api/test-edge endpoint.
    """
    if not WEASYPRINT_AVAILABLE:
        return {"status": "failed", "error": "WeasyPrint not installed"}

    test_html = (
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<style>@page { size: A4; margin: 2cm; }'
        'body { font-family: sans-serif; font-size: 24px; }</style>'
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
    """
    Convert HTML to PDF and return as download.

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
