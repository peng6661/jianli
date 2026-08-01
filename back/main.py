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

    Two modes based on the HTML content:
    - Default view (has .resume-paper, no .paginated-page):
      Backend measures content height and generates a single-page PDF
      with explicit width/height parameters.
    - Paginated view (has .paginated-page):
      Uses standard A4 format for multi-page PDF.

    Critical: measurement is done in PRINT media context with A4-width
    viewport, matching the rendering context used by page.pdf().
    This eliminates layout differences between screen and print that
    caused content to overflow and paginate with large images.
    """
    page = None
    try:
        browser = await get_browser()
        page = await browser.new_page()

        # 1. Set viewport to A4 dimensions (210mm x 297mm at 96dpi).
        #    This ensures the layout at measurement time matches the
        #    layout at PDF generation time.
        await page.set_viewport_size({"width": 794, "height": 1123})

        # 2. Switch to print media BEFORE setting content.
        #    page.pdf() renders in print context; if we measure in screen
        #    context, the layout may differ (font rendering, @media print
        #    rules, sub-pixel rounding), causing the measured height to
        #    be slightly shorter than the actual print height → overflow
        #    → unwanted pagination.
        await page.emulate_media("print")

        # 3. Set HTML content — domcontentloaded is sufficient since all
        #    resources (images) are base64 inline, no external requests
        await page.set_content(html, wait_until="domcontentloaded")

        # 4. Wait for all images to fully load and decode.
        #    Although images have explicit width/height (layout is stable),
        #    we still wait to ensure fonts and images are fully rendered
        #    before measuring.
        try:
            await page.evaluate(
                """async () => {
                    const imgs = Array.from(document.querySelectorAll('img'));
                    await Promise.all(imgs.map(img => {
                        if (img.complete && img.naturalWidth > 0) return Promise.resolve();
                        return new Promise(res => {
                            img.addEventListener('load', res, {once: true});
                            img.addEventListener('error', res, {once: true});
                        });
                    }));
                    await Promise.all(imgs.map(img => {
                        if (img.decode) return img.decode().catch(() => {});
                        return Promise.resolve();
                    }));
                    // Wait for layout to settle
                    await new Promise(res => {
                        requestAnimationFrame(() => requestAnimationFrame(res));
                    });
                }""",
                timeout=15000,
            )
        except Exception as e:
            log.warning("Image decode wait timed out: %s", str(e))

        # 5. Wait for fonts to be ready
        try:
            await page.evaluate("document.fonts.ready")
        except Exception:
            pass

        # 6. Determine view mode and generate PDF
        has_paginated = await page.evaluate(
            "document.querySelector('.paginated-page') !== null"
        )

        if has_paginated:
            # Paginated view: standard A4 multi-page
            # Use explicit format parameter — bypass @page CSS entirely
            pdf_bytes = await page.pdf(
                print_background=True,
                format="A4",
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            )
        else:
            # Default view: single-page PDF with height = content height
            # Measure twice with a short delay to ensure layout stability
            h1 = await page.evaluate(
                """() => {
                    const r = document.querySelector('.resume-paper');
                    return r ? r.scrollHeight : 0;
                }"""
            )
            await page.wait_for_timeout(200)
            h2 = await page.evaluate(
                """() => {
                    const r = document.querySelector('.resume-paper');
                    return r ? r.scrollHeight : 0;
                }"""
            )
            # Use the larger value + buffer to account for any remaining
            # sub-pixel differences between measurement and PDF rendering
            height_px = max(h1, h2) + 20

            if height_px > 20:
                height_mm = height_px / 96 * 25.4
                height_mm = max(height_mm, 10)
                log.info(
                    "Default view: measured h1=%d h2=%d, using %dpx = %.1fmm",
                    h1, h2, height_px, height_mm,
                )
                # Use explicit width/height — bypass @page CSS entirely.
                # This is more reliable than prefer_css_page_size=True
                # because there's no ambiguity about which @page rule wins.
                pdf_bytes = await page.pdf(
                    print_background=True,
                    width="210mm",
                    height=f"{height_mm:.1f}mm",
                    margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                )
            else:
                # Fallback: A4
                pdf_bytes = await page.pdf(
                    print_background=True,
                    format="A4",
                    margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                )

        log.info("PDF generated: %s (%d bytes)", filename, len(pdf_bytes))
        return pdf_bytes

    except Exception as e:
        log.error("PDF generation failed: %s", str(e))
        raise RuntimeError(f"PDF generation failed: {str(e)}") from e

    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


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
