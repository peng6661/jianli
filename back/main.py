#!/usr/bin/env python3
"""
Resume Editor - PDF Export Service (Cloud Edition)

Architecture: localStorage cloud deployment = static site + lightweight backend
- Uses Playwright (Chromium) for cross-platform PDF rendering
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
# Playwright Browser (lazy init, reused across requests)
# ============================================
from playwright.async_api import async_playwright

_playwright = None
_browser = None


async def get_browser():
    """Get or create a shared browser instance."""
    global _playwright, _browser
    if _browser and _browser.is_connected():
        return _browser
    if _playwright is None:
        _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
    )
    log.info("Playwright Chromium browser launched")
    return _browser


# ============================================
# FastAPI App
# ============================================
app = FastAPI(
    title="Resume Editor PDF Service",
    description="Playwright-based HTML-to-PDF conversion (cloud edition)",
    version="2.0.0",
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
    browser_ready: bool


# ============================================
# PDF Export Core
# ============================================
async def convert_html_to_pdf(html: str, filename: str) -> bytes:
    """
    Render HTML to PDF using Playwright's Chromium engine.

    The HTML may contain:
    - @page CSS rules (A4 size, margins, page-break)
    - Inline JS that waits for images to decode, measures content height,
      sets dynamic @page size, then signals via window.__pdfPageReady
    - -webkit-print-color-adjust: exact for color fidelity

    We wait for the embedded JS signal (window.__pdfPageReady) instead of
    a fixed delay, ensuring images are fully decoded before height measurement.
    """
    try:
        browser = await get_browser()
        page = await browser.new_page()

        # Set the HTML content (domcontentloaded is enough — no external resources
        # in the export HTML, all images are base64 inline)
        await page.set_content(html, wait_until="domcontentloaded")

        # Wait for embedded JS to signal completion.
        # The JS waits for all images to decode, measures content height,
        # sets the dynamic @page size, then sets window.__pdfPageReady = true.
        # This replaces the unreliable fixed-delay approach (wait_for_timeout)
        # which failed when large base64 images took longer to decode.
        try:
            await page.wait_for_function(
                "window.__pdfPageReady === true", timeout=15000
            )
        except Exception:
            log.warning("PDF page-ready signal timeout, using fallback page size")

        # Ensure fonts are loaded
        try:
            await page.evaluate("document.fonts.ready")
        except Exception:
            pass

        # Generate PDF with background colors preserved
        # prefer_css_page_size=True: 让 HTML 中的 @page CSS 规则决定页面大小
        #   - 默认视图: JS 动态设置 @page{size:210mm <内容高度>mm} → 单页
        #   - 分页视图: @page{size:A4} → 标准 A4 多页
        pdf_bytes = await page.pdf(
            print_background=True,
            prefer_css_page_size=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
        )

        await page.close()

        log.info("PDF generated: %s (%d bytes)", filename, len(pdf_bytes))
        return pdf_bytes

    except Exception as e:
        log.error("PDF generation failed: %s", str(e))
        raise RuntimeError(f"PDF generation failed: {str(e)}") from e


# ============================================
# API Endpoints
# ============================================
@app.get("/api/health", response_model=HealthResponse)
async def health_check():
    """Health check - verify Playwright browser is available."""
    try:
        browser = await get_browser()
        return HealthResponse(status="ok", browser_ready=browser is not None)
    except Exception:
        return HealthResponse(status="degraded", browser_ready=False)


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

    try:
        pdf_bytes = await convert_html_to_pdf(html, filename)
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
# Startup / Shutdown
# ============================================
@app.on_event("shutdown")
async def shutdown_event():
    """Clean up browser resources on shutdown."""
    global _browser, _playwright
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None
    log.info("Playwright resources cleaned up")


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
    print(f"  CORS:     {_allowed_origins}")
    print()
    print("  PDF Engine: Playwright (Chromium)")
    print()
    print("  Press Ctrl+C to stop")
    print()

    uvicorn.run(app, host=host, port=port, log_level="info")
