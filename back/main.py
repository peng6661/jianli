#!/usr/bin/env python3
"""
Resume Editor - PDF Export Service (Cloud Edition)

Architecture: localStorage cloud deployment = static site + lightweight backend
- Uses Microsoft Edge headless + Xvfb for PDF rendering (same engine as local deployment)
- Edge headless requires --headless flag for --print-to-pdf to work
- Xvfb provides DISPLAY=:99 which helps headless mode initialize in Docker
- Edge's --virtual-time-budget ensures all async operations (image decode, fonts)
  complete before PDF generation, eliminating pagination issues with large images
- No Git API, no database, stateless
- CORS configurable via environment variable
"""

import os
import sys
import time
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
    description="Edge headless+Xvfb HTML-to-PDF conversion (cloud edition)",
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
    Convert HTML to PDF using Edge headless + Xvfb.

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

        # Build command line arguments.
        # --headless=old: old headless mode (simpler than --headless=new, doesn't use
        #   Aura window system). --headless=new caused SIGTRAP crash in single-process
        #   mode (V8 Proxy resolver error + crashpad).
        # --single-process: runs all Edge code in ONE process (browser+renderer+GPU).
        #   Without it, Edge spawns 5+ processes → OOM killer (server has 266MB free, 0 swap).
        # --proxy-server=direct://: attempt to bypass V8 Proxy resolver (may not fully work).
        cmd = [
            EDGE_PATH,
            "--headless=old",
            "--single-process",
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
            "--disable-features=TranslateUI,Translate,OptimizationGuide,OptimizationHints",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-crash-reporter",
            "--disable-component-update",
            "--proxy-server=direct://",
            "--user-data-dir=/tmp/edge-profile",
            "--virtual-time-budget=10000",
            "--print-to-pdf-no-header",
            f"--print-to-pdf={temp_pdf}",
            Path(temp_html).absolute().as_uri(),
        ]

        log.info("Generating PDF via Edge headless+Xvfb: %s", filename)
        log.info("Edge command: %s", " ".join(cmd))

        # Explicitly set D-Bus env vars for Edge subprocess.
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

        # Edge headless may not exit on its own; poll for PDF file.
        # When --virtual-time-budget completes, PDF is written but process
        # may linger. Kill it once PDF is ready.
        pdf_path = Path(temp_pdf)
        stdout_data = ""
        deadline = time.time() + 30

        while True:
            # Did Edge exit on its own?
            if process.poll() is not None:
                try:
                    stdout_data, _ = process.communicate(timeout=5)
                except Exception:
                    pass
                break
            # Is the PDF ready?
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                time.sleep(0.5)  # Ensure file is fully written
                process.kill()
                try:
                    stdout_data, _ = process.communicate(timeout=5)
                except Exception:
                    pass
                log.info("PDF ready, Edge terminated")
                break
            # Timeout?
            if time.time() >= deadline:
                process.kill()
                try:
                    stdout_data, _ = process.communicate(timeout=5)
                except Exception:
                    pass
                log.error("Edge TIMED OUT (30s). Output:\n%s",
                          stdout_data[:3000] if stdout_data else "(empty)")
                _kill_edge_processes()
                break
            time.sleep(0.5)

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
            "--headless=old",
            "--single-process",
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
            "--disable-features=TranslateUI,Translate,OptimizationGuide,OptimizationHints",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-crash-reporter",
            "--disable-component-update",
            "--proxy-server=direct://",
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

        # Poll for PDF file (same as main export)
        pdf_path = Path(temp_pdf)
        stdout_data = ""
        deadline = time.time() + 30

        while True:
            if process.poll() is not None:
                try:
                    stdout_data, _ = process.communicate(timeout=5)
                except Exception:
                    pass
                break
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                time.sleep(0.5)
                process.kill()
                try:
                    stdout_data, _ = process.communicate(timeout=5)
                except Exception:
                    pass
                break
            if time.time() >= deadline:
                process.kill()
                try:
                    stdout_data, _ = process.communicate(timeout=5)
                except Exception:
                    pass
                log.error("[test-edge] TIMEOUT (30s). Output:\n%s",
                          stdout_data[:2000] if stdout_data else "(empty)")
                break
            time.sleep(0.5)

        pdf_path = Path(temp_pdf)
        pdf_exists = pdf_path.exists()
        pdf_size = pdf_path.stat().st_size if pdf_exists else 0

        result = {
            "status": "ok" if pdf_exists and pdf_size > 0 else "failed",
            "exit_code": process.returncode,
            "stdout": stdout_data[:3000] if stdout_data else "",
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


@app.get("/api/diag-edge")
def diag_edge():
    """
    Comprehensive Edge diagnostics for Docker containers.
    Checks D-Bus, shared libraries, and runs a --dump-dom test.
    """

    result = {
        "edge_path": EDGE_PATH,
        "checks": {},
    }

    if EDGE_PATH is None:
        result["checks"]["edge_found"] = False
        return result

    result["checks"]["edge_found"] = True

    # 1. Edge version
    try:
        ver = subprocess.run(
            [EDGE_PATH, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        result["checks"]["edge_version"] = ver.stdout.strip() or ver.stderr.strip()
    except Exception as e:
        result["checks"]["edge_version"] = f"error: {e}"

    # 2. DBUS env vars
    result["checks"]["env"] = {
        "DBUS_SYSTEM_BUS_ADDRESS": os.environ.get("DBUS_SYSTEM_BUS_ADDRESS", "(unset)"),
        "DBUS_SESSION_BUS_ADDRESS": os.environ.get("DBUS_SESSION_BUS_ADDRESS", "(unset)"),
    }

    # 3. dbus-daemon process running? (scan /proc, no external deps)
    dbus_running = False
    dbus_pid = None
    try:
        for proc_dir in Path("/proc").iterdir():
            if proc_dir.name.isdigit():
                comm_file = proc_dir / "comm"
                if comm_file.exists():
                    comm = comm_file.read_text(errors="replace").strip()
                    if "dbus-daemon" in comm:
                        dbus_running = True
                        dbus_pid = proc_dir.name
                        break
    except Exception:
        pass
    result["checks"]["dbus_daemon_running"] = dbus_running
    if dbus_pid:
        result["checks"]["dbus_daemon_pid"] = dbus_pid

    # 4. Socket exists?
    socket_path = Path("/run/dbus/system_bus_socket")
    result["checks"]["dbus_socket_exists"] = socket_path.exists()
    if socket_path.exists():
        result["checks"]["dbus_socket"] = str(socket_path)

    # 5. Missing shared libraries
    try:
        ldd = subprocess.run(
            ["ldd", EDGE_PATH],
            capture_output=True, text=True, timeout=10,
        )
        missing = [l for l in ldd.stdout.splitlines() if "not found" in l]
        result["checks"]["missing_libs"] = missing if missing else []
        result["checks"]["ldd_ok"] = len(missing) == 0
    except Exception as e:
        result["checks"]["missing_libs"] = f"error: {e}"

    # 5.5. Check /dev/shm size and container capabilities
    try:
        df = subprocess.run(["df", "-h", "/dev/shm"], capture_output=True, text=True, timeout=5)
        result["checks"]["dev_shm"] = df.stdout.strip()
    except Exception as e:
        result["checks"]["dev_shm"] = f"error: {e}"

    try:
        cap = subprocess.run(["cat", "/proc/self/status"], capture_output=True, text=True, timeout=5)
        cap_lines = [l for l in cap.stdout.splitlines() if "Cap" in l]
        result["checks"]["capabilities"] = cap_lines
    except Exception as e:
        result["checks"]["capabilities"] = f"error: {e}"

    # 5.6. Font inventory — list installed font files and sizes.
    #      Edge loads ALL fonts on startup; large fonts cause timeouts.
    try:
        font_files = []
        font_dirs = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts")]
        for font_dir in font_dirs:
            if font_dir.exists():
                for f in font_dir.rglob("*"):
                    if f.is_file() and f.suffix in (".ttc", ".ttf", ".otf"):
                        stat = f.stat()
                        font_files.append({
                            "path": str(f),
                            "size_mb": round(stat.st_size / 1048576, 1),
                        })
        total_mb = round(sum(f["size_mb"] for f in font_files), 1)
        result["checks"]["fonts"] = {
            "total_files": len(font_files),
            "total_mb": total_mb,
            "files": font_files[:20],
        }
    except Exception as e:
        result["checks"]["fonts"] = {"error": str(e)}

    # 5.7. Memory check — OOM killer can cause SIGKILL (-9)
    try:
        meminfo = Path("/proc/meminfo").read_text()
        mem_lines = [l for l in meminfo.splitlines()
                     if l.startswith(("MemTotal", "MemFree", "MemAvailable", "SwapTotal", "SwapFree"))]
        result["checks"]["memory"] = mem_lines
    except Exception as e:
        result["checks"]["memory"] = f"error: {e}"

    # 6. PDF generation tests with multiple configurations.
    #    Test 1 (headless_new): no --no-zygote + proxy fix (the new approach)
    #    Test 2 (headless_no_zygote): with --no-zygote (old approach, for comparison)
    #    Test 3 (headless_verbose): verbose logging to diagnose if still failing
    test_html = (
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        "</head><body><h1>Diag Test</h1></body></html>"
    )
    edge_env = os.environ.copy()
    edge_env["DBUS_SYSTEM_BUS_ADDRESS"] = "unix:path=/run/dbus/system_bus_socket"
    edge_env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/dbus/system_bus_socket"

    # Common flags for all tests (--no-zygote removed: zygote works now that
    # shm=2gb + seccomp=unconfined are set; --proxy-server=direct:// bypasses
    # V8 proxy resolver init that hangs in Docker)
    common_flags = [
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--password-store=basic",
        "--disable-background-networking",
        "--disable-crash-reporter",
        "--proxy-server=direct://",
        "--virtual-time-budget=10000",
        "--print-to-pdf-no-header",
    ]

    # Three test configurations:
    # 1. headless_old_single: --headless=old + --single-process (old headless is simpler,
    #    doesn't use Aura window system, might avoid SIGTRAP crash)
    # 2. headless_new_verbose: --headless=new + --single-process + very verbose logging
    #    (--v=2) to capture the full crash sequence
    # 3. headless_old_multi: --headless=old without --single-process (old headless uses
    #    fewer processes than new, might not OOM with 266MB free)
    pdf_tests = [
        ("headless_old_single", ["--headless=old", "--single-process"]),
        ("headless_new_verbose", ["--headless=new", "--single-process", "--enable-logging=stderr", "--v=2"]),
        ("headless_old_multi", ["--headless=old"]),
    ]

    for test_name, extra_flags in pdf_tests:
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
                *extra_flags,
                *common_flags,
                f"--user-data-dir=/tmp/edge-diag-{test_name}",
                f"--print-to-pdf={temp_pdf}",
                Path(temp_html).absolute().as_uri(),
            ]

            process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=edge_env,
            )

            # Poll for PDF file (up to 15s)
            pdf_path = Path(temp_pdf)
            stdout_data = ""
            deadline = time.time() + 15

            while True:
                if process.poll() is not None:
                    try:
                        stdout_data, _ = process.communicate(timeout=5)
                    except Exception:
                        pass
                    break
                if pdf_path.exists() and pdf_path.stat().st_size > 0:
                    time.sleep(0.5)
                    process.kill()
                    try:
                        stdout_data, _ = process.communicate(timeout=5)
                    except Exception:
                        pass
                    break
                if time.time() >= deadline:
                    process.kill()
                    try:
                        stdout_data, _ = process.communicate(timeout=5)
                    except Exception:
                        pass
                    break
                time.sleep(0.5)

            pdf_exists = pdf_path.exists()
            pdf_size = pdf_path.stat().st_size if pdf_exists else 0

            result["checks"][f"pdf_{test_name}"] = {
                "exit_code": process.returncode,
                "stdout_preview": (stdout_data[:8000] if stdout_data else "(empty)"),
                "pdf_created": pdf_exists,
                "pdf_size": pdf_size,
            }
        except Exception as e:
            result["checks"][f"pdf_{test_name}"] = {"status": "error", "message": str(e)}
        finally:
            if temp_html:
                Path(temp_html).unlink(missing_ok=True)
            if temp_pdf:
                Path(temp_pdf).unlink(missing_ok=True)

    return result


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
    print("  PDF Engine: Microsoft Edge headless + Xvfb")
    if EDGE_PATH:
        print(f"  Edge:     {EDGE_PATH}")
    else:
        print("  WARNING: Edge not found, PDF export unavailable")
    print()
    print("  Press Ctrl+C to stop")
    print()

    uvicorn.run(app, host=host, port=port, log_level="info")
