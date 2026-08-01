#!/usr/bin/env python3
"""
Resume Editor - PDF Export Service (Cloud Edition)

Dual-engine architecture:
  Primary: Microsoft Edge headless (Blink engine = same as browser preview)
  Fallback: WeasyPrint (pure Python, ~50MB, for when Edge is unavailable)

Edge produces PDFs that match the browser preview exactly because it uses
the same rendering engine (Blink). WeasyPrint is kept as a fallback for
environments where Edge cannot run.

A 2GB swap file is created at container startup (see docker-entrypoint.sh)
to provide Edge with enough virtual memory on resource-constrained servers.
"""

import os
import re
import sys
import math
import time
import shutil
import logging
import tempfile
import subprocess
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

# WeasyPrint (fallback engine)
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
# Edge binary detection
# ============================================
EDGE_CANDIDATES = [
    "/usr/bin/microsoft-edge-stable",
    "/usr/bin/microsoft-edge",
    "/opt/microsoft/msedge/microsoft-edge",
]


def find_edge() -> Optional[str]:
    """Find Edge binary on Linux."""
    for path in EDGE_CANDIDATES:
        if Path(path).exists():
            log.info("Edge found: %s", path)
            return path
    # Try which command
    try:
        result = subprocess.run(
            ["which", "microsoft-edge-stable"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            p = result.stdout.strip()
            if p and Path(p).exists():
                log.info("Edge found (which): %s", p)
                return p
    except Exception:
        pass
    log.warning("Edge not found, will use WeasyPrint fallback")
    return None


EDGE_PATH = find_edge()


def get_edge_version() -> Optional[str]:
    """Get Edge version string."""
    if not EDGE_PATH:
        return None
    try:
        result = subprocess.run(
            [EDGE_PATH, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


# ============================================
# FastAPI App
# ============================================
app = FastAPI(
    title="Resume Editor PDF Service",
    description="Edge (primary) + WeasyPrint (fallback) PDF conversion",
    version="6.0.0",
)

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
    edge_available: bool = False
    edge_version: Optional[str] = None
    weasyprint_available: bool = False
    weasyprint_version: Optional[str] = None


# ============================================
# Shared CSS overrides
# ============================================
# Font override: --ant-font uses macOS/Windows fonts that don't exist
# in the Docker container. Replace with Liberation Sans (Arial-compatible
# metrics) + WenQuanYi Zen Hei (CJK). This makes Edge render closer to
# what the user sees in their browser.
# Also remove preview-only visual styles (border, shadow, rounded corners).
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


def _inject_css(html: str, css: str) -> str:
    """Inject a <style> block at the end of <head>."""
    style_tag = f"<style>{css}</style>"
    if "</head>" in html:
        return html.replace("</head>", f"{style_tag}</head>", 1)
    return style_tag + html


def _is_paginated_view(html: str) -> bool:
    """Check if the HTML uses A4 page size (paginated view)."""
    return bool(re.search(r"@page\s*\{[^}]*size:\s*A4", html, re.IGNORECASE))


# ============================================
# Edge PDF conversion (primary engine)
# ============================================
def _convert_with_edge(html: str, filename: str) -> bytes:
    """Convert HTML to PDF using Edge headless.

    Uses the same simple flags as the local Windows deployment.
    Edge executes JavaScript, so the frontend's embedded re-measurement
    script works correctly (measures actual content height, sets @page size).

    With 2GB swap (created at container startup), Edge has enough virtual
    memory to run in normal multi-process mode.
    """
    if not EDGE_PATH:
        raise RuntimeError("Edge binary not found")

    temp_html = None
    temp_pdf = None

    try:
        # Write HTML to temp file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as f:
            f.write(html)
            temp_html = f.name

        # PDF output path (Edge won't overwrite existing files)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            temp_pdf = f.name
        if Path(temp_pdf).exists():
            Path(temp_pdf).unlink()

        # Build command — same simple flags as local Windows version
        # --headless=new: required for --print-to-pdf
        # --virtual-time-budget=10000: fast-forwards time so images load
        # --disable-dev-shm-usage: use /tmp instead of /dev/shm
        # No --single-process, no --no-zygote — normal multi-process mode
        # works fine with swap providing virtual memory.
        cmd = [
            EDGE_PATH,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-sync",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
            "--disable-crash-reporter",
            "--virtual-time-budget=10000",
            "--print-to-pdf-no-header",
            f"--print-to-pdf={temp_pdf}",
            Path(temp_html).absolute().as_uri(),
        ]

        log.info("[Edge] Generating PDF: %s", filename)

        # Set up environment with D-Bus addresses
        env = os.environ.copy()
        env["DBUS_SYSTEM_BUS_ADDRESS"] = "unix:path=/run/dbus/system_bus_socket"
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/dbus/system_bus_socket"

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
        )

        try:
            stdout_data, _ = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            # Check if PDF was created before timeout (Edge sometimes
            # generates the PDF but doesn't exit cleanly)
            if Path(temp_pdf).exists() and Path(temp_pdf).stat().st_size > 0:
                log.warning("[Edge] Process timed out but PDF was created")
            else:
                raise RuntimeError("Edge timed out (60s), no PDF generated")

        exit_code = process.returncode

        # Check if PDF was created (Edge may log errors but still produce PDF)
        if Path(temp_pdf).exists() and Path(temp_pdf).stat().st_size > 0:
            pdf_bytes = Path(temp_pdf).read_bytes()
            log.info("[Edge] PDF generated: %d bytes (exit_code=%d)", len(pdf_bytes), exit_code)
            return pdf_bytes

        # PDF not created
        stdout_preview = stdout_data[:1000] if stdout_data else "(empty)"
        raise RuntimeError(
            f"Edge exited with code {exit_code}, no PDF generated. "
            f"stdout: {stdout_preview}"
        )

    finally:
        # Clean up temp files
        for path in [temp_html, temp_pdf]:
            if path and Path(path).exists():
                try:
                    Path(path).unlink()
                except Exception:
                    pass


# ============================================
# WeasyPrint PDF conversion (fallback engine)
# ============================================
def _remove_scripts(html: str) -> str:
    """Remove all <script> tags. WeasyPrint does not execute JavaScript."""
    return re.sub(
        r"<script[^>]*>.*?</script>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )


def _measure_content_height_mm(html: str) -> int:
    """Render with a very tall page, traverse box tree to find exact height."""
    measure_html = _inject_css(html, "@page { size: 210mm 100000mm; margin: 0; }")

    try:
        doc = HTML(string=measure_html).render()
    except Exception as e:
        log.warning("[WeasyPrint] Measure render failed: %s, using A4 fallback", e)
        return 297

    if not doc.pages:
        return 297

    max_bottom_px = 0

    def visit_box(box):
        nonlocal max_bottom_px
        y = getattr(box, "position_y", 0) or 0
        h = getattr(box, "height", 0) or 0
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
        pb = getattr(page, "_page_box", None)
        if pb is None:
            pb = getattr(page, "page_box", None)
        if pb is not None:
            visit_box(pb)

    if max_bottom_px > 0:
        height_mm = math.ceil(max_bottom_px / 3.7795) + 2
        log.info("[WeasyPrint] Content height: %dpx -> %dmm", max_bottom_px, height_mm)
        return height_mm

    log.warning("[WeasyPrint] Box tree traversal returned 0, using A4 fallback")
    try:
        a4_html = _inject_css(html, "@page { size: 210mm 297mm; margin: 0; }")
        a4_doc = HTML(string=a4_html).render()
        num_pages = len(a4_doc.pages)
        height_mm = num_pages * 297
        return height_mm
    except Exception:
        return 297


def _convert_with_weasyprint(html: str, filename: str) -> bytes:
    """Convert HTML to PDF using WeasyPrint with two-pass rendering.

    For default (single-page) view:
      Pass 1: Render with tall page, measure content height via box tree.
      Pass 2: Re-render with @page { size: 210mm <measured>mm }.
    For paginated view: Render directly with A4.
    """
    if not WEASYPRINT_AVAILABLE:
        raise RuntimeError("WeasyPrint not installed")

    log.info("[WeasyPrint] Generating PDF: %s", filename)

    # Remove scripts (WeasyPrint ignores JS)
    html_clean = _remove_scripts(html)

    # Inject font + visual overrides
    html_styled = _inject_css(html_clean, _PDF_OVERRIDE_CSS)

    is_paginated = _is_paginated_view(html_styled)

    if is_paginated:
        log.info("[WeasyPrint] Paginated view: A4")
        pdf_bytes = HTML(string=html_styled).write_pdf()
    else:
        height_mm = _measure_content_height_mm(html_styled)
        log.info("[WeasyPrint] Default view: single page, height=%dmm", height_mm)
        final_html = _inject_css(
            html_styled,
            f"@page {{ size: 210mm {height_mm}mm; margin: 0; }}",
        )
        pdf_bytes = HTML(string=final_html).write_pdf()

    if not pdf_bytes or len(pdf_bytes) == 0:
        raise RuntimeError("WeasyPrint produced empty PDF")

    log.info("[WeasyPrint] PDF generated: %d bytes", len(pdf_bytes))
    return pdf_bytes


# ============================================
# Main conversion function (dual-engine)
# ============================================
def convert_html_to_pdf(html: str, filename: str) -> bytes:
    """Convert HTML to PDF.

    Primary engine: Edge (Blink) — produces PDF matching browser preview.
    Fallback engine: WeasyPrint — pure Python, lower memory, slightly different.

    For Edge: HTML is passed with font override CSS injected. Edge executes
    the frontend's embedded JS to re-measure content height and set @page size.
    For WeasyPrint: Scripts removed, two-pass rendering measures height via
    box tree traversal.
    """
    # Inject font override CSS for both engines (makes Docker fonts match
    # Arial metrics, producing layout closer to browser preview)
    html_styled = _inject_css(html, _PDF_OVERRIDE_CSS)

    # Try Edge first
    if EDGE_PATH:
        try:
            return _convert_with_edge(html_styled, filename)
        except Exception as e:
            log.warning("[Edge] Failed: %s, falling back to WeasyPrint", e)

    # Fallback to WeasyPrint
    if WEASYPRINT_AVAILABLE:
        return _convert_with_weasyprint(html_styled, filename)

    raise RuntimeError("No PDF engine available (Edge not found, WeasyPrint not installed)")


# ============================================
# API Endpoints
# ============================================
@app.get("/api/health", response_model=HealthResponse)
def health_check():
    """Health check - report available PDF engines."""
    edge_ver = get_edge_version() if EDGE_PATH else None
    return HealthResponse(
        status="ok" if (EDGE_PATH or WEASYPRINT_AVAILABLE) else "degraded",
        engine="edge" if EDGE_PATH else ("weasyprint" if WEASYPRINT_AVAILABLE else "none"),
        edge_available=bool(EDGE_PATH),
        edge_version=edge_ver,
        weasyprint_available=WEASYPRINT_AVAILABLE,
        weasyprint_version=WEASYPRINT_VERSION,
    )


@app.get("/api/test-pdf")
def test_pdf():
    """Test PDF generation with the primary engine."""
    test_html = (
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<style>@page { size: A4; margin: 2cm; }'
        "body { font-family: sans-serif; font-size: 24px; }</style>"
        "</head><body><h1>PDF Test</h1>"
        "<p>Hello from Docker</p>"
        '<p style="color: #1677ff;">Color test</p>'
        "</body></html>"
    )

    try:
        # Try Edge first
        if EDGE_PATH:
            try:
                pdf_bytes = _convert_with_edge(test_html, "test")
                return {
                    "status": "ok",
                    "engine": "edge",
                    "edge_version": get_edge_version(),
                    "pdf_size": len(pdf_bytes),
                }
            except Exception as e:
                log.warning("[test-pdf] Edge failed: %s, trying WeasyPrint", e)

        # Fallback
        if WEASYPRINT_AVAILABLE:
            pdf_bytes = HTML(string=test_html).write_pdf()
            return {
                "status": "ok",
                "engine": "weasyprint",
                "version": WEASYPRINT_VERSION,
                "pdf_size": len(pdf_bytes),
            }

        return {"status": "failed", "error": "No PDF engine available"}
    except Exception as e:
        log.error("[test-pdf] Exception: %s", str(e), exc_info=True)
        return {"status": "error", "message": str(e)}


# Backward compatibility alias
@app.get("/api/test-edge")
def test_edge_alias():
    """Alias for /api/test-pdf."""
    return test_pdf()


@app.post("/api/export-pdf")
async def export_pdf(request: PdfExportRequest):
    """Convert HTML to PDF and return as download."""
    html = request.html
    filename = request.filename

    if not html or not html.strip():
        raise HTTPException(status_code=400, detail="HTML content cannot be empty")

    if not EDGE_PATH and not WEASYPRINT_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="PDF export service not ready (no engine available)",
        )

    try:
        pdf_bytes = convert_html_to_pdf(html, filename)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

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
    if EDGE_PATH:
        print(f"  Primary:  Edge ({get_edge_version() or 'version unknown'})")
    else:
        print("  Primary:  Edge (NOT FOUND)")
    if WEASYPRINT_AVAILABLE:
        print(f"  Fallback: WeasyPrint v{WEASYPRINT_VERSION}")
    else:
        print("  Fallback: WeasyPrint (not installed)")
    print()
    print("  Press Ctrl+C to stop")
    print()

    uvicorn.run(app, host=host, port=port, log_level="info")
