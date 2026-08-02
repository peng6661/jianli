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
import json
import signal
import base64
import shutil
import logging
import tempfile
import subprocess
import asyncio
import urllib.request
from pathlib import Path
from typing import Optional

import websockets
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
    version="7.1.0",
)

# ============================================
# Concurrency control
# ============================================
# Edge multi-process mode uses ~100-150 MB per request.
# 8GB RAM + 1GB swap. Limit concurrent Edge instances to 8
# (8 × 150MB = 1.2GB, leaving ~6.8GB headroom).
# Override via env var if needed.
MAX_CONCURRENT_PDFS = int(os.environ.get("MAX_CONCURRENT_PDFS", "2"))

# Virtual time budget for Edge (ms). Controls how long Edge fast-forwards
# time for async operations (image loading, JS timers).
# Frontend now pre-calculates exact @page height — no JS execution needed in Edge.
# 500ms is ample for font loading and initial render. Override via env var.
EDGE_VIRTUAL_TIME_BUDGET = int(os.environ.get("EDGE_VIRTUAL_TIME_BUDGET", "500"))

# Per-request timeout (seconds). Edge should finish well within this.
# With CDP mode, typical render time is <3s. Subprocess fallback may
# take longer due to cold start. 20s is a generous upper bound.
EDGE_TIMEOUT = int(os.environ.get("EDGE_TIMEOUT", "20"))

# Semaphore — initialised lazily (asyncio.Semaphore needs a running loop)
_pdf_semaphore: Optional[asyncio.Semaphore] = None


def _get_semaphore() -> asyncio.Semaphore:
    """Get or lazily create the concurrency semaphore."""
    global _pdf_semaphore
    if _pdf_semaphore is None:
        _pdf_semaphore = asyncio.Semaphore(MAX_CONCURRENT_PDFS)
    return _pdf_semaphore

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
    mode: str = "subprocess"
    edge_available: bool = False
    edge_version: Optional[str] = None


# ============================================
# Edge temp file cleanup
# ============================================
def cleanup_edge_temp_files():
    """Remove leftover Edge temp files from previous runs.

    Edge can leave behind profile directories, socket files, and crash dumps
    in /tmp. These accumulate over time and waste container memory.
    Called:
    - At the start of each PDF request (lightweight, catches orphans).
    - On server startup (one-time sweep).
    """
    patterns = [
        "/tmp/edge-profile-*",                # Profile dirs from crashed requests
        "/tmp/.org.chromium.Chromium.*",       # Singleton socket leftovers
        "/tmp/.com.microsoft.Edge.*",          # Edge socket leftovers
        "/tmp/tmp*.html",                      # Orphaned HTML temp files
        "/tmp/tmp*.pdf",                       # Orphaned PDF temp files
    ]
    cleaned = 0
    for pattern in patterns:
        for p in Path("/").glob(pattern.lstrip("/")):
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
                cleaned += 1
            except Exception:
                pass
    if cleaned > 0:
        log.info("[Cleanup] Removed %d leftover temp files/dirs", cleaned)


# ============================================
# Persistent Edge Browser (CDP via WebSocket)
# ============================================
class EdgeCDPClient:
    """Persistent Edge browser managed via Chrome DevTools Protocol.

    Edge stays alive across requests — eliminates cold-start penalty (~1-2s).
    Each PDF request creates a new CDP target, navigates to the HTML, calls
    Page.printToPDF, then closes the target.

    Falls back to subprocess mode if CDP is unavailable.
    """

    def __init__(self, edge_path: str, debug_port: int = 9222):
        self.edge_path = edge_path
        self.debug_port = debug_port
        self.process: Optional[subprocess.Popen] = None
        self._msg_id = 0
        self._ready = False

    # ---- Edge lifecycle ----

    def start(self):
        """Launch Edge in headless mode with remote debugging enabled."""
        if self._ready:
            return

        # Ensure port is free (kill any stale Edge on this port)
        self._kill_stale()

        user_data_dir = tempfile.mkdtemp(prefix="edge-persist-")
        cmd = [
            self.edge_path,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-sync",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-crash-reporter",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-features=TranslateUI,BackForwardCache",
            "--enable-features=NetworkServiceInProcess",
            f"--user-data-dir={user_data_dir}",
            f"--remote-debugging-port={self.debug_port}",
            "about:blank",  # Keep a blank page open
        ]

        env = os.environ.copy()
        env["DBUS_SYSTEM_BUS_ADDRESS"] = "unix:path=/run/dbus/system_bus_socket"
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/dbus/system_bus_socket"

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )

        # Wait for debug port to be ready (retry up to 30s)
        # On resource-constrained servers, Edge cold start can take
        # 15-25 seconds. 30s is a generous upper bound.
        last_error = None
        for i in range(120):  # 120 × 0.25s = 30s
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.debug_port}/json/version",
                    timeout=2,
                )
                self._ready = True
                log.info("[CDP] Edge browser ready on port %d (pid=%d, attempt=%d)",
                         self.debug_port, self.process.pid, i + 1)
                return
            except Exception as e:
                last_error = e
                time.sleep(0.25)
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"Edge exited prematurely (code={self.process.returncode})"
                )

        raise RuntimeError(
            f"Edge did not start within 30 seconds. "
            f"Last error: {last_error}"
        )

    def _kill_stale(self):
        """Kill any process holding the debug port."""
        try:
            result = subprocess.run(
                ["fuser", f"{self.debug_port}/tcp"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                pids = result.stdout.strip().split()
                for pid in pids:
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                    except Exception:
                        pass
                time.sleep(0.5)
        except Exception:
            pass

    def _get_ws_url(self) -> str:
        """Get the browser WebSocket debugger URL."""
        try:
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{self.debug_port}/json/version",
                timeout=5,
            )
            data = json.loads(resp.read().decode())
            return data["webSocketDebuggerUrl"]
        except Exception as e:
            raise RuntimeError(f"Failed to get CDP WebSocket URL: {e}")

    def ensure_alive(self):
        """Check if Edge is still running; restart if crashed."""
        if self.process is None or self.process.poll() is not None:
            log.warning("[CDP] Edge process died, restarting...")
            self._ready = False
            self.start()
            return
        # Quick health check
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.debug_port}/json/version",
                timeout=2,
            )
        except Exception:
            log.warning("[CDP] Edge unresponsive, restarting...")
            self._ready = False
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            except Exception:
                pass
            self.start()

    def close(self):
        """Shut down Edge."""
        if self.process and self.process.poll() is None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            except Exception:
                pass
            self.process.wait()
        self._ready = False

    # ---- PDF generation via CDP ----

    async def generate_pdf(self, html: str, filename: str) -> bytes:
        """Generate PDF using CDP Page.printToPDF.

        Uses a temp HTML file + file:// URI (no base64 overhead, no size limit).
        Polls document.readyState instead of waiting for CDP events (avoids the
        race condition where _cdp_call consumes loadEventFired before the event
        listener starts).
        """
        t_start = time.monotonic()
        self.ensure_alive()
        ws_url = self._get_ws_url()

        log.info("[CDP] Generating PDF: %s (html=%d bytes)", filename, len(html))

        temp_html = None
        try:
            async with websockets.connect(
                ws_url,
                max_size=10 * 1024 * 1024,
                ping_interval=None,
                close_timeout=5,
            ) as ws:
                t_conn = time.monotonic()

                # 1. Create a new target (tab)
                target_id = await self._cdp_call(ws, "Target.createTarget", {
                    "url": "about:blank",
                    "newWindow": False,
                })
                target_id = target_id["targetId"]
                t_target = time.monotonic()

                # 2. Attach to the target
                session = await self._cdp_call(ws, "Target.attachToTarget", {
                    "targetId": target_id,
                    "flatten": True,
                })
                session_id = session["sessionId"]

                try:
                    # 3. Enable domains
                    await self._cdp_call(ws, "Page.enable", {}, session_id)
                    await self._cdp_call(ws, "Runtime.enable", {}, session_id)

                    # 4. Write HTML to temp file (avoid data: URI base64 + size limit)
                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".html", delete=False, encoding="utf-8"
                    ) as f:
                        f.write(html)
                        temp_html = f.name
                    file_url = Path(temp_html).absolute().as_uri()

                    # 5. Navigate to file:// URI
                    nav_result = await self._cdp_call(ws, "Page.navigate", {
                        "url": file_url,
                    }, session_id)

                    if nav_result.get("errorText"):
                        raise RuntimeError(
                            f"Page navigation failed: {nav_result['errorText']}"
                        )
                    t_nav = time.monotonic()

                    # 6. Poll document.readyState until "complete"
                    #    (avoids event race condition — _cdp_call would consume
                    #     Page.loadEventFired before a separate event listener sees it)
                    for poll_i in range(80):  # 80 × 100ms = 8s max
                        await asyncio.sleep(0.1)
                        try:
                            ready = await self._cdp_call(ws, "Runtime.evaluate", {
                                "expression": "document.readyState",
                                "returnByValue": True,
                            }, session_id, timeout=5)
                            if ready.get("result", {}).get("value") == "complete":
                                break
                        except Exception:
                            pass  # retry on evaluate failure
                    t_load = time.monotonic()

                    # 7. Generate PDF
                    pdf_result = await self._cdp_call(
                        ws, "Page.printToPDF", {
                            "printBackground": True,
                            "preferCSSPageSize": True,
                            "displayHeaderFooter": False,
                            "transferMode": "ReturnAsBase64",
                        },
                        session_id,
                        timeout=30,
                    )
                    t_pdf = time.monotonic()

                    pdf_bytes = base64.b64decode(pdf_result["data"])

                    log.info(
                        "[CDP] PDF ready: %d bytes | "
                        "conn=%.0fms target=%.0fms nav=%.0fms load=%.0fms pdf=%.0fms total=%.0fms",
                        len(pdf_bytes),
                        (t_conn - t_start) * 1000,
                        (t_target - t_conn) * 1000,
                        (t_nav - t_target) * 1000,
                        (t_load - t_nav) * 1000,
                        (t_pdf - t_load) * 1000,
                        (t_pdf - t_start) * 1000,
                    )
                    return pdf_bytes

                finally:
                    try:
                        await self._cdp_call(ws, "Target.closeTarget", {
                            "targetId": target_id,
                        }, timeout=3)
                    except Exception:
                        pass

        except Exception:
            total_elapsed = time.monotonic() - t_start
            log.error(
                "[CDP] Failed after %.1fs, falling back to subprocess",
                total_elapsed, exc_info=True,
            )
            return _convert_with_edge_subprocess(html, filename)
        finally:
            # Clean up temp HTML file
            if temp_html and Path(temp_html).exists():
                try:
                    Path(temp_html).unlink()
                except Exception:
                    pass

    # ---- CDP helpers ----

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _cdp_call(self, ws, method: str, params: dict = None,
                        session_id: str = None, timeout: int = 15) -> dict:
        """Send a CDP command and return the result."""
        msg_id = self._next_id()
        msg = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        await ws.send(json.dumps(msg))

        # Read responses until we get the matching id
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"CDP timeout: {method}")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 5))
            except asyncio.TimeoutError:
                continue
            resp = json.loads(raw)
            if resp.get("id") == msg_id:
                if "error" in resp:
                    raise RuntimeError(
                        f"CDP error: {resp['error'].get('message', resp['error'])}"
                    )
                return resp.get("result", {})
            # else: event or response for a different command — skip

        raise RuntimeError(f"CDP timeout waiting for {method}")

# Global CDP client instance (initialized on startup)
_edge_cdp: Optional[EdgeCDPClient] = None


# ============================================
# Edge PDF conversion (subprocess fallback)
# ============================================
def _convert_with_edge_subprocess(html: str, filename: str) -> bytes:
    """[FALLBACK] Convert HTML to PDF using a one-shot Edge subprocess.

    Used only when the persistent CDP browser is unavailable.
    Each call launches a fresh Edge process, generates PDF, then exits.
    """
    if not EDGE_PATH:
        raise RuntimeError("Edge binary not found")

    t0 = time.monotonic()
    # Clean up any leftover temp files from previously crashed/timed-out
    # requests before starting a new Edge process.
    cleanup_edge_temp_files()

    temp_html = None
    temp_pdf = None
    user_data_dir = None

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

        # Unique user-data-dir per request — prevents file-lock conflicts
        # when multiple Edge instances run concurrently.
        user_data_dir = tempfile.mkdtemp(prefix="edge-profile-")

        # Build command — same simple flags as local Windows version
        # --headless=new: required for --print-to-pdf
        # --virtual-time-budget: fast-forwards time so images load
        # --disable-dev-shm-usage: use /tmp instead of /dev/shm (safer in Docker)
        # No --single-process — it's unstable with headless mode and can
        #   cause crashes. Multi-process mode is more reliable.
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
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-features=TranslateUI,BackForwardCache",
            f"--user-data-dir={user_data_dir}",
            f"--virtual-time-budget={EDGE_VIRTUAL_TIME_BUDGET}",
            "--print-to-pdf-no-header",
            f"--print-to-pdf={temp_pdf}",
            Path(temp_html).absolute().as_uri(),
        ]

        log.info("[Edge] Generating PDF: %s (budget=%dms)", filename, EDGE_VIRTUAL_TIME_BUDGET)

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
            start_new_session=True,  # New process group → killpg kills all children
        )
        t_spawn = time.monotonic()

        try:
            stdout_data, _ = process.communicate(timeout=EDGE_TIMEOUT)
        except subprocess.TimeoutExpired:
            t_timeout = time.monotonic()
            log.warning("[Edge] Timed out after %.1fs (limit=%ds, spawn=%.1fs), killing process group...",
                       t_timeout - t0, EDGE_TIMEOUT, t_spawn - t0)
            # Kill first — don't read stdout yet; process.stdout.read()
            # blocks until pipe close (Edge exit), which could take 60s+
            # if Edge is stuck in D-state (uninterruptible sleep).
            stdout_data = b"(timed out before reading stdout)"
            try:
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                process.kill()
            # Secondary timeout: process.wait() can hang if Edge processes
            # are stuck in D-state (uninterruptible sleep, e.g. swap thrashing).
            # SIGKILL can't kill D-state processes — don't wait forever.
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.error(
                    "[Edge] Process refused to die after SIGKILL (D-state?). "
                    "Giving up on waiting, checking for PDF anyway..."
                )
            t_killed = time.monotonic()
            # Check if PDF was created before timeout (Edge sometimes
            # generates the PDF but doesn't exit cleanly)
            if Path(temp_pdf).exists() and Path(temp_pdf).stat().st_size > 0:
                log.warning("[Edge] Process timed out but PDF was created "
                           "(timeout=%.1fs, kill=%.1fs, total=%.1fs)",
                           t_timeout - t0, t_killed - t_timeout, t_killed - t0)
            else:
                partial = stdout_data[:2000].decode("utf-8", errors="replace") if stdout_data else "(no output)"
                log.error("[Edge] Timeout (%ds). Partial output:\n%s", EDGE_TIMEOUT, partial)
                raise RuntimeError(f"Edge timed out ({EDGE_TIMEOUT}s), no PDF generated. Output: {partial[:500]}")

        exit_code = process.returncode

        # Check if PDF was created (Edge may log errors but still produce PDF)
        if Path(temp_pdf).exists() and Path(temp_pdf).stat().st_size > 0:
            pdf_bytes = Path(temp_pdf).read_bytes()
            t_done = time.monotonic()
            log.info("[Edge] PDF generated: %d bytes (exit_code=%d, spawn=%.1fs, total=%.1fs)",
                     len(pdf_bytes), exit_code, t_spawn - t0, t_done - t0)
            return pdf_bytes

        # PDF not created
        stdout_preview = stdout_data[:1000] if stdout_data else "(empty)"
        raise RuntimeError(
            f"Edge exited with code {exit_code}, no PDF generated. "
            f"stdout: {stdout_preview}"
        )

    finally:
        # Clean up temp HTML and PDF files
        for path in [temp_html, temp_pdf]:
            if path and Path(path).exists():
                try:
                    Path(path).unlink()
                except Exception:
                    pass

        # Clean up user-data-dir (can be large, contains Edge profile data)
        if user_data_dir and Path(user_data_dir).exists():
            try:
                shutil.rmtree(user_data_dir, ignore_errors=True)
            except Exception:
                pass


# ============================================
# Main conversion function
# ============================================
async def convert_html_to_pdf(html: str, filename: str) -> bytes:
    """Convert HTML to PDF using persistent Edge via CDP.

    Uses the CDP-based persistent browser for fast PDF generation
    (no cold start). Falls back to subprocess mode if CDP is unavailable.
    """
    global _edge_cdp
    if _edge_cdp is not None:
        try:
            return await _edge_cdp.generate_pdf(html, filename)
        except Exception:
            log.error("[CDP] Failed, falling back to subprocess", exc_info=True)
    # Fallback: one-shot subprocess
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _convert_with_edge_subprocess, html, filename
    )


# ============================================
# API Endpoints
# ============================================
@app.get("/api/health", response_model=HealthResponse)
def health_check():
    """Health check - report Edge availability and CDP status."""
    edge_ver = get_edge_version() if EDGE_PATH else None
    cdp_ready = _edge_cdp is not None and _edge_cdp._ready
    return HealthResponse(
        status="ok" if (EDGE_PATH and cdp_ready) else ("degraded" if EDGE_PATH else "unavailable"),
        engine="edge" if EDGE_PATH else "none",
        mode="cdp" if cdp_ready else "subprocess",
        edge_available=bool(EDGE_PATH),
        edge_version=edge_ver,
    )


@app.get("/api/test-pdf")
async def test_pdf():
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
            pdf_bytes = await convert_html_to_pdf(test_html, "test")
            return {
                "status": "ok",
                "engine": "edge",
                "mode": "cdp" if (_edge_cdp and _edge_cdp._ready) else "subprocess",
                "edge_version": get_edge_version(),
                "pdf_size": len(pdf_bytes),
            }

        return {"status": "failed", "error": "Edge not available"}
    except Exception as e:
        log.error("[test-pdf] Exception: %s", str(e), exc_info=True)
        return {"status": "error", "message": str(e)}


@app.get("/api/diag-edge")
def diag_edge():
    """Diagnose Edge issues — runs Edge with verbose logging."""
    if not EDGE_PATH:
        return {"status": "error", "message": "Edge not found"}

    import tempfile, json

    # 1. Check memory
    meminfo = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip()
                    if key in ("MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree", "Shmem"):
                        meminfo[key] = val
    except Exception:
        pass

    # 2. Check /dev/shm size
    shm_size = ""
    try:
        result = subprocess.run(["df", "-h", "/dev/shm"], capture_output=True, text=True, timeout=5)
        shm_size = result.stdout.strip()
    except Exception:
        pass

    # 3. Try running Edge with verbose logging
    temp_html = None
    temp_pdf = None
    edge_output = ""
    exit_code = None

    try:
        # Simple test HTML
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
            f.write('<!DOCTYPE html><html><body><h1>Test</h1></body></html>')
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
            "--disable-extensions",
            "--disable-sync",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-crash-reporter",
            "--enable-logging=stderr",
            "--v=1",
            "--virtual-time-budget=5000",
            "--print-to-pdf-no-header",
            f"--print-to-pdf={temp_pdf}",
            Path(temp_html).absolute().as_uri(),
        ]

        env = os.environ.copy()
        env["DBUS_SYSTEM_BUS_ADDRESS"] = "unix:path=/run/dbus/system_bus_socket"
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/dbus/system_bus_socket"

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
                env=env, stdin=subprocess.DEVNULL,
            )
            edge_output = (result.stdout + "\n" + result.stderr)[-3000:]
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            edge_output = "TIMEOUT after 30s"

        pdf_exists = Path(temp_pdf).exists() if temp_pdf else False
        pdf_size = Path(temp_pdf).stat().st_size if pdf_exists else 0

        return {
            "status": "ok" if pdf_size > 0 else "failed",
            "memory": meminfo,
            "shm": shm_size,
            "edge_cmd": " ".join(cmd),
            "exit_code": exit_code,
            "pdf_created": pdf_exists,
            "pdf_size": pdf_size,
            "edge_output": edge_output[-2000:],
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "edge_output": edge_output}
    finally:
        for p in [temp_html, temp_pdf]:
            if p and Path(p).exists():
                try:
                    Path(p).unlink()
                except Exception:
                    pass


# Backward compatibility alias
@app.get("/api/test-edge")
def test_edge_alias():
    """Alias for /api/test-pdf."""
    return test_pdf()


@app.post("/api/export-pdf")
async def export_pdf(request: PdfExportRequest):
    """Convert HTML to PDF and return as download.

    Uses persistent Edge via CDP for fast generation (no cold start).
    Concurrency is controlled by a semaphore (MAX_CONCURRENT_PDFS).
    """
    html = request.html
    filename = request.filename

    if not html or not html.strip():
        raise HTTPException(status_code=400, detail="HTML content cannot be empty")

    if not EDGE_PATH:
        raise HTTPException(
            status_code=503,
            detail="PDF export service not ready (Edge not available)",
        )

    # Acquire semaphore — limits concurrent CDP operations
    sem = _get_semaphore()
    async with sem:
        try:
            pdf_bytes = await convert_html_to_pdf(html, filename)
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

    # One-time cleanup of leftover temp files from previous container runs
    cleanup_edge_temp_files()

    # Pre-launch persistent Edge browser (CDP mode)
    if EDGE_PATH:
        try:
            _edge_cdp = EdgeCDPClient(EDGE_PATH, debug_port=9222)
            _edge_cdp.start()
            log.info("Edge CDP browser pre-launched — PDF requests will be fast!")
        except Exception as e:
            log.warning("CDP pre-launch failed, will use subprocess fallback: %s", e)
            _edge_cdp = None
    else:
        _edge_cdp = None

    print("=" * 50)
    print("  Resume Editor PDF Service (Cloud Edition)")
    print("=" * 50)
    print()
    print(f"  API:       http://{host}:{port}")
    print(f"  Health:    http://{host}:{port}/api/health")
    print(f"  Export:    POST http://{host}:{port}/api/export-pdf")
    print(f"  Test:      GET http://{host}:{port}/api/test-pdf")
    print(f"  CORS:      {_allowed_origins}")
    print(f"  Engine:    {'CDP (persistent)' if (_edge_cdp and _edge_cdp._ready) else 'Subprocess (one-shot)'}")
    print(f"  Concurrency: max {MAX_CONCURRENT_PDFS} parallel requests")
    print()
    if EDGE_PATH:
        print(f"  Edge:      {get_edge_version() or 'version unknown'}")
    else:
        print("  Edge:      NOT FOUND")
    print()
    print("  Press Ctrl+C to stop")
    print()

    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        if _edge_cdp:
            log.info("Shutting down Edge CDP browser...")
            _edge_cdp.close()
