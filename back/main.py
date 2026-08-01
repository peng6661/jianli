#!/usr/bin/env python3
"""
Resume Editor - PDF Export Service (Cloud Edition)

Architecture: localStorage cloud deployment = static site + lightweight backend
- Uses Microsoft Edge headless for PDF rendering (same engine as local deployment)
- Edge's --virtual-time-budget ensures all async operations (image decode, fonts)
  complete before PDF generation, eliminating pagination issues with large images
- No Git API, no database, stateless
- CORS configurable via environment variable
"""

import os
import sys
import subprocess
import tempfile
import shutil
import logging
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
# FastAPI App
# ============================================
app = FastAPI(
    title="Resume Editor PDF Service",
    description="Edge headless HTML-to-PDF conversion (cloud edition)",
    version="3.0.0",
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
# Edge Browser Discovery
# ============================================
EDGE_PATHS = [
    # Linux (Debian/Ubuntu - installed via Microsoft apt repo)
    "/usr/bin/microsoft-edge-stable",
    "/usr/bin/microsoft-edge",
    "/opt/microsoft/msedge/microsoft-edge",
    # Windows (for local development)
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
]


def find_edge() -> Optional[str]:
    """Find Edge browser executable on the system."""
    # 1. Check known paths
    for path in EDGE_PATHS:
        if path and Path(path).exists():
            log.info("Edge found: %s", path)
            return path

    # 2. Try `which` command (Linux) or `where` (Windows)
    search_cmd = ["which", "microsoft-edge-stable"] if sys.platform != "win32" else ["where", "msedge"]
    try:
        result = subprocess.run(
            search_cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line and Path(line).exists():
                    log.info("Edge found (search): %s", line)
                    return line
    except Exception:
        pass

    log.warning("Edge browser not found, PDF export unavailable")
    return None


EDGE_PATH = find_edge()


# ============================================
# Request / Response Models
# ============================================
class PdfExportRequest(BaseModel):
    html: str
    filename: str = "Resume Editor"


class HealthResponse(BaseModel):
    status: str
    edge_found: bool
    edge_path: Optional[str] = None


# ============================================
# PDF Export Core
# ============================================
def convert_html_to_pdf(html: str, filename: str) -> bytes:
    """
    Convert HTML to PDF using Edge headless mode.

    Uses Edge's --virtual-time-budget to fast-forward all async operations
    (image decode, font loading, setTimeout), ensuring the page is fully
    rendered before PDF generation. This is the same mechanism used by
    the local deployment and eliminates pagination issues with large images.

    The @page CSS rules embedded in the HTML control page size:
    - Default view: @page{size:210mm <H>mm} (single page, height = content)
    - Paginated view: @page{size:A4} (standard multi-page A4)

    An embedded <script> in the HTML re-measures content height in Edge's
    rendering context and overrides @page for maximum accuracy.
    """
    global EDGE_PATH

    if EDGE_PATH is None:
        EDGE_PATH = find_edge()
    if EDGE_PATH is None:
        raise RuntimeError("Edge browser not found. Please install Microsoft Edge")

    temp_html = None
    temp_pdf = None

    try:
        # Write HTML to temp file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as f:
            f.write(html)
            temp_html = f.name

        # PDF output temp file
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            temp_pdf = f.name
        # Edge headless won't overwrite existing file, ensure it's removed
        Path(temp_pdf).unlink(missing_ok=True)

        # Build command line arguments
        cmd = [
            EDGE_PATH,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-software-rasterizer",
            "--disable-extensions",
            "--disable-sync",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
            "--disable-features=TranslateUI,Translate",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--user-data-dir=/tmp/edge-profile",
            "--virtual-time-budget=10000",
            "--print-to-pdf-no-header",
            f"--print-to-pdf={temp_pdf}",
            Path(temp_html).absolute().as_uri(),
        ]

        log.info("Generating PDF via Edge headless: %s", filename)
        log.info("Edge command: %s", " ".join(cmd))

        # Explicitly set D-Bus env vars for Edge subprocess.
        # Docker ENV sets these globally, but we ensure them here too
        # in case the parent process environment was modified.
        edge_env = os.environ.copy()
        edge_env["DBUS_SYSTEM_BUS_ADDRESS"] = "unix:path=/run/dbus/system_bus_socket"
        edge_env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/dbus/system_bus_socket"

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=edge_env,
        )

        try:
            stdout_data, _ = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout_data, _ = process.communicate()  # Read remaining output after kill
            log.error("Edge TIMED OUT (30s). Edge output:\n%s",
                      stdout_data[:3000] if stdout_data else "(empty)")
            # Kill any lingering Edge processes (Linux: pkill, Windows: taskkill)
            _kill_edge_processes()
            raise RuntimeError(
                "Edge browser timed out (30s). "
                f"Edge output: {stdout_data[:500] if stdout_data else '(empty)'}"
            )

        exit_code = process.returncode
        if exit_code != 0:
            log.warning("Edge exit code: %d, output: %s", exit_code, stdout_data)
            # Even with non-zero exit code, PDF may still be generated

        # Check if PDF was generated successfully
        pdf_path = Path(temp_pdf)
        if not pdf_path.exists() or pdf_path.stat().st_size == 0:
            raise RuntimeError("Edge failed to generate PDF file")

        pdf_bytes = pdf_path.read_bytes()
        log.info("PDF generated: %s (%d bytes)", filename, len(pdf_bytes))
        return pdf_bytes

    except Exception as e:
        log.error("PDF generation failed: %s", str(e))
        raise RuntimeError(f"PDF generation failed: {str(e)}") from e

    finally:
        # Clean up temp files
        for tmp_file in [temp_html, temp_pdf]:
            if tmp_file:
                try:
                    Path(tmp_file).unlink(missing_ok=True)
                except Exception:
                    pass


def _kill_edge_processes():
    """Kill lingering Edge processes (cross-platform)."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/f", "/im", "msedge.exe"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
    else:
        try:
            subprocess.run(
                ["pkill", "-f", "microsoft-edge"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass


# ============================================
# API Endpoints
# ============================================
@app.get("/api/health", response_model=HealthResponse)
def health_check():
    """Health check - verify Edge browser is available."""
    return HealthResponse(
        status="ok" if EDGE_PATH else "degraded",
        edge_found=EDGE_PATH is not None,
        edge_path=EDGE_PATH,
    )


@app.get("/api/test-edge")
def test_edge():
    """Minimal Edge test - generate a simple PDF to verify Edge works in this container."""
    if EDGE_PATH is None:
        raise HTTPException(status_code=503, detail="Edge browser not found")

    test_html = (
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<style>body{font-size:24px;}</style>'
        "</head><body><h1>Edge Test</h1><p>Hello from Docker</p></body></html>"
    )

    temp_html = None
    temp_pdf = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as f:
            f.write(test_html)
            temp_html = f.name

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            temp_pdf = f.name
        Path(temp_pdf).unlink(missing_ok=True)

        cmd = [
            EDGE_PATH,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-software-rasterizer",
            "--disable-extensions",
            "--disable-sync",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
            "--disable-features=TranslateUI,Translate",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--user-data-dir=/tmp/edge-test-profile",
            "--virtual-time-budget=10000",
            "--print-to-pdf-no-header",
            f"--print-to-pdf={temp_pdf}",
            Path(temp_html).absolute().as_uri(),
        ]

        log.info("[test-edge] Running: %s", " ".join(cmd))

        # Same D-Bus env as main export
        edge_env = os.environ.copy()
        edge_env["DBUS_SYSTEM_BUS_ADDRESS"] = "unix:path=/run/dbus/system_bus_socket"
        edge_env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/dbus/system_bus_socket"

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=edge_env,
        )

        try:
            stdout_data, _ = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout_data, _ = process.communicate()
            log.error("[test-edge] TIMEOUT (15s). Output:\n%s",
                      stdout_data[:2000] if stdout_data else "(empty)")
            return {
                "status": "timeout",
                "exit_code": None,
                "stdout": stdout_data[:1000] if stdout_data else "",
                "pdf_created": False,
            }

        pdf_path = Path(temp_pdf)
        pdf_exists = pdf_path.exists()
        pdf_size = pdf_path.stat().st_size if pdf_exists else 0

        result = {
            "status": "ok" if pdf_exists and pdf_size > 0 else "failed",
            "exit_code": process.returncode,
            "stdout": stdout_data[:1000] if stdout_data else "",
            "pdf_created": pdf_exists,
            "pdf_size": pdf_size,
        }
        log.info("[test-edge] Result: %s", result)
        return result

    except Exception as e:
        log.error("[test-edge] Exception: %s", str(e))
        return {"status": "error", "message": str(e)}
    finally:
        for tmp in [temp_html, temp_pdf]:
            if tmp:
                Path(tmp).unlink(missing_ok=True)



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

    if EDGE_PATH is None:
        raise HTTPException(
            status_code=503,
            detail="PDF export service not ready (Microsoft Edge not installed)",
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
    print(f"  CORS:     {_allowed_origins}")
    print()
    print("  PDF Engine: Microsoft Edge headless")
    if EDGE_PATH:
        print(f"  Edge:     {EDGE_PATH}")
    else:
        print("  WARNING: Edge not found, PDF export unavailable")
    print()
    print("  Press Ctrl+C to stop")
    print()

    uvicorn.run(app, host=host, port=port, log_level="info")
