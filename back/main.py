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
      Backend measures content height AFTER images decode, injects a
      dynamic @page{size:210mm <height>mm} for a single-page PDF.
    - Paginated view (has .paginated-page):
      Uses the static @page{size:A4} from CSS for multi-page PDF.

    The height measurement is done by the backend directly on the Playwright
    page (not embedded JS), eliminating timing issues with image decoding.
    """
    page = None
    try:
        browser = await get_browser()
        page = await browser.new_page()

        # 1. Set HTML content — domcontentloaded is enough since all
        #    resources (images) are base64 inline, no external requests
        await page.set_content(html, wait_until="domcontentloaded")

        # 2. Wait for all images to fully load and decode.
        #    This is the critical step — base64 images decode asynchronously,
        #    and measuring height before decode completes gives wrong results.
        #    We replicate Edge's --virtual-time-budget behavior by explicitly
        #    waiting for image decode to finish.
        try:
            await page.evaluate(
                """async () => {
                    const imgs = Array.from(document.querySelectorAll('img'));
                    // Step 1: wait for all images to fire load/error
                    await Promise.all(imgs.map(img => {
                        if (img.complete && img.naturalWidth > 0) return Promise.resolve();
                        return new Promise(res => {
                            img.addEventListener('load', res, {once: true});
                            img.addEventListener('error', res, {once: true});
                        });
                    }));
                    // Step 2: wait for decode to complete
                    await Promise.all(imgs.map(img => {
                        if (img.decode) return img.decode().catch(() => {});
                        return Promise.resolve();
                    }));
                    // Step 3: wait for layout to settle (two RAF cycles)
                    await new Promise(res => {
                        requestAnimationFrame(() => requestAnimationFrame(res));
                    });
                }""",
                timeout=15000,
            )
        except Exception as e:
            log.warning("Image decode wait timed out: %s", str(e))

        # 3. Wait for fonts to be ready
        try:
            await page.evaluate("document.fonts.ready")
        except Exception:
            pass

        # 4. Default view: measure content height and set dynamic @page size.
        #    Paginated view: @page{size:A4} is already in the CSS, skip.
        has_paginated = await page.evaluate(
            "document.querySelector('.paginated-page') !== null"
        )
        if not has_paginated:
            # Measure the .resume-paper content height in the Playwright page
            height_px = await page.evaluate(
                """() => {
                    const r = document.querySelector('.resume-paper');
                    if (!r) return 0;
                    // Use scrollHeight which includes all content + padding
                    // Add small buffer (6px) to avoid rounding cutoff
                    return r.scrollHeight + 6;
                }"""
            )
            if height_px and height_px > 0:
                # Convert px to mm (96dpi: 1mm = 96/25.4 px ≈ 3.779 px)
                height_mm = height_px / 96 * 25.4
                height_mm = max(height_mm, 10)  # safety floor
                await page.evaluate(
                    f"""() => {{
                        const s = document.createElement('style');
                        s.textContent = '@page{{size:210mm {height_mm:.1f}mm;margin:0}}';
                        document.head.appendChild(s);
                    }}"""
                )
                log.info(
                    "Default view: measured %dpx = %.1fmm, set single-page @page",
                    height_px, height_mm,
                )

        # 5. Generate PDF
        #    prefer_css_page_size=True: use the @page rule from CSS
        #      - Default view: dynamically injected @page{size:210mm <height>mm}
        #      - Paginated view: static @page{size:A4}
        pdf_bytes = await page.pdf(
            print_background=True,
            prefer_css_page_size=True,
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
