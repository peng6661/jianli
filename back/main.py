#!/usr/bin/env python3
"""
Resume Editor - PDF Export Service (Cloud Edition)

Uses Microsoft Edge headless (Blink engine) to render HTML to PDF.
Edge produces PDFs that match the browser preview exactly because it uses
the same rendering engine (Blink).

A 2GB swap file is created at container startup (see docker-entrypoint.sh)
to provide Edge with enough virtual memory on resource-constrained servers.
"""

import os
import sys
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
    log.warning("Edge not found, PDF export unavailable")
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
    description="Edge headless PDF conversion (Blink = same as browser preview)",
    version="7.0.0",
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


# ============================================
# Edge PDF conversion
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
# Main conversion function
# ============================================
def convert_html_to_pdf(html: str, filename: str) -> bytes:
    """Convert HTML to PDF using Edge (Blink engine).

    Edge produces PDFs matching the browser preview exactly because it uses
    the same rendering engine. The frontend's embedded JS re-measures content
    height and sets @page size, which Edge executes correctly.
    """
    # Inject font override CSS (makes Docker fonts match Arial metrics,
    # producing layout closer to browser preview)
    html_styled = _inject_css(html, _PDF_OVERRIDE_CSS)

    return _convert_with_edge(html_styled, filename)


# ============================================
# API Endpoints
# ============================================
@app.get("/api/health", response_model=HealthResponse)
def health_check():
    """Health check - report Edge availability."""
    edge_ver = get_edge_version() if EDGE_PATH else None
    return HealthResponse(
        status="ok" if EDGE_PATH else "degraded",
        engine="edge" if EDGE_PATH else "none",
        edge_available=bool(EDGE_PATH),
        edge_version=edge_ver,
    )


@app.get("/api/test-pdf")
def test_pdf():
    """Test PDF generation with Edge."""
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
        if EDGE_PATH:
            pdf_bytes = _convert_with_edge(test_html, "test")
            return {
                "status": "ok",
                "engine": "edge",
                "edge_version": get_edge_version(),
                "pdf_size": len(pdf_bytes),
            }

        return {"status": "failed", "error": "Edge not available"}
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

    if not EDGE_PATH:
        raise HTTPException(
            status_code=503,
            detail="PDF export service not ready (Edge not available)",
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
        print(f"  Engine:   Edge ({get_edge_version() or 'version unknown'})")
    else:
        print("  Engine:   Edge (NOT FOUND)")
    print()
    print("  Press Ctrl+C to stop")
    print()

    uvicorn.run(app, host=host, port=port, log_level="info")
