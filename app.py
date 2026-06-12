"""
Aurelia local Python sandbox — sidecar service (design.md §4.5).

Speaks the tiny 3-endpoint HTTP protocol that `server/internal/sandbox/
sandbox.go` already expects, so the Go backend needs zero changes — just point
SANDBOX_BASE_URL at this service:

    POST /sessions  -> {"session_id": "..."}
    POST /exec      {session_id, code, timeout_ms}
                    -> {"stdout", "stderr", "exit_code", "files":[{name,mime_type,data_base64}]}
    POST /files     {session_id, path, data_base64}  -> {"ok": true}

Each session is one long-lived, locked-down Docker container running the
`aurelia-sandbox` image (see Dockerfile.runner). /workspace persists across
exec calls within a session — pip-installed packages, generated files and
intermediate data survive, matching ChatGPT Code Interpreter behaviour.

Design baselines honoured here (§4.5 安全基线):
  - non-root, --network none by default, no-new-privileges, dropped caps
  - memory / cpu / pids limits
  - 120s exec timeout (overridable per-call, capped)
  - stdout/stderr truncated to 32KB before returning to the model
  - single artifact file capped at 20MB
  - 30min idle TTL reaper

This is a DEV / single-host tool. For production swap `docker run` here for a
gVisor / microVM / E2B backend — the Go side and this HTTP contract do not move.
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
import os
import re
import subprocess
import threading
import time
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# --- Config (env-overridable) ----------------------------------------------
IMAGE = os.environ.get("SANDBOX_IMAGE", "aurelia-sandbox:latest")
NETWORK = os.environ.get("SANDBOX_NETWORK", "none")  # set "bridge" to allow pip at runtime
MEMORY = os.environ.get("SANDBOX_MEMORY", "2g")       # §4.5 doc rendering can be heavy
CPUS = os.environ.get("SANDBOX_CPUS", "1")
PIDS_LIMIT = os.environ.get("SANDBOX_PIDS_LIMIT", "256")
API_KEY = os.environ.get("SANDBOX_API_KEY", "")       # if set, require Bearer match
EXEC_TIMEOUT_CAP_MS = int(os.environ.get("SANDBOX_EXEC_TIMEOUT_CAP_MS", "120000"))
IDLE_TTL_SECONDS = int(os.environ.get("SANDBOX_IDLE_TTL_SECONDS", "1800"))  # 30 min
# When truthy, `docker pull` the runtime image once on startup so a fresh
# server doesn't fail the first /sessions call. Best-effort: logs and continues.
PULL_ON_START = os.environ.get("SANDBOX_PULL_ON_START", "") not in ("", "0", "false")

MAX_OUTPUT_BYTES = 32 * 1024          # stdout/stderr truncation
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024  # single produced file cap

CONTAINER_PREFIX = "aurelia-sbx-"
LABEL = "aurelia.sandbox=1"
WORKSPACE = "/workspace"
OUTPUTS_DIR = f"{WORKSPACE}/outputs"
UPLOADS_DIR = f"{WORKSPACE}/uploads"
CELL_PATH = f"{WORKSPACE}/.cell.py"

# session_id -> last-used epoch seconds (for the idle reaper). Container state
# itself lives in Docker, so a sidecar restart only loses TTL tracking, not
# sessions.
_last_used: dict[str, float] = {}
_lock = threading.Lock()

app = FastAPI(title="Aurelia Sandbox Sidecar", version="1.0")


# --- Docker helpers ---------------------------------------------------------
def _container(session_id: str) -> str:
    return CONTAINER_PREFIX + session_id


def _valid_session(session_id: str) -> bool:
    # session ids we mint are uuid4 hex; never interpolate anything else into a
    # container name / shell.
    return bool(re.fullmatch(r"[0-9a-f]{32}", session_id or ""))


def _docker(args: list[str], *, input_bytes: Optional[bytes] = None,
            timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
    )


def _is_running(session_id: str) -> bool:
    cp = _docker(["inspect", "-f", "{{.State.Running}}", _container(session_id)])
    return cp.returncode == 0 and cp.stdout.strip() == b"true"


def _truncate(b: bytes) -> str:
    if len(b) > MAX_OUTPUT_BYTES:
        b = b[:MAX_OUTPUT_BYTES] + b"\n... [truncated, output exceeded 32KB]"
    return b.decode("utf-8", errors="replace")


# --- Request models ---------------------------------------------------------
class ExecBody(BaseModel):
    session_id: str
    code: str
    timeout_ms: Optional[int] = None


class FilesBody(BaseModel):
    session_id: str
    path: str
    data_base64: str


# --- Endpoints --------------------------------------------------------------
@app.post("/sessions")
def create_session():
    # Auth (when SANDBOX_API_KEY is set) is enforced in the middleware below.
    session_id = uuid.uuid4().hex
    name = _container(session_id)
    args = [
        "run", "-d",
        "--name", name,
        "--label", LABEL,
        "--network", NETWORK,
        "--memory", MEMORY,
        "--cpus", CPUS,
        "--pids-limit", PIDS_LIMIT,
        "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL",
        "-w", WORKSPACE,
        IMAGE,
        "sleep", "infinity",
    ]
    cp = _docker(args, timeout=60)
    if cp.returncode != 0:
        raise HTTPException(status_code=500, detail=f"docker run failed: {cp.stderr.decode(errors='replace')}")
    # Make sure the standard dirs exist (image already creates them, but be safe).
    _docker(["exec", name, "mkdir", "-p", UPLOADS_DIR, OUTPUTS_DIR], timeout=20)
    with _lock:
        _last_used[session_id] = time.time()
    return {"session_id": session_id}


@app.post("/exec")
def exec_code(body: ExecBody):
    sid = body.session_id
    if not _valid_session(sid):
        raise HTTPException(status_code=400, detail="invalid session_id")
    if not _is_running(sid):
        raise HTTPException(status_code=404, detail="session not found or not running")
    name = _container(sid)

    timeout_ms = body.timeout_ms or EXEC_TIMEOUT_CAP_MS
    timeout_ms = max(1000, min(timeout_ms, EXEC_TIMEOUT_CAP_MS))
    timeout_s = timeout_ms / 1000.0

    # Write the cell to a file in the container (stdin avoids arg-length limits
    # and shell-quoting hazards).
    w = _docker(["exec", "-i", name, "sh", "-c", f"cat > {CELL_PATH}"],
                input_bytes=body.code.encode("utf-8"), timeout=30)
    if w.returncode != 0:
        raise HTTPException(status_code=500, detail=f"write cell failed: {w.stderr.decode(errors='replace')}")

    before = _snapshot_outputs(name)

    # `timeout` (coreutils) kills runaway code inside the container; we add a
    # small host-side margin so docker-exec returns even if the kill races.
    exit_code = 0
    try:
        cp = _docker(
            ["exec", name, "timeout", "--signal=KILL", str(int(timeout_s)), "python", CELL_PATH],
            timeout=timeout_s + 15,
        )
        exit_code = cp.returncode
        stdout, stderr = cp.stdout, cp.stderr
    except subprocess.TimeoutExpired:
        exit_code = 124
        stdout, stderr = b"", b""

    if exit_code == 124:  # `timeout` convention
        stderr = (stderr or b"") + f"\n[sandbox] execution exceeded {int(timeout_s)}s and was killed".encode()

    with _lock:
        _last_used[sid] = time.time()

    files = _collect_new_files(name, before)
    return {
        "stdout": _truncate(stdout or b""),
        "stderr": _truncate(stderr or b""),
        "exit_code": exit_code,
        "files": files,
    }


@app.post("/files")
def put_file(body: FilesBody):
    sid = body.session_id
    if not _valid_session(sid):
        raise HTTPException(status_code=400, detail="invalid session_id")
    if not _is_running(sid):
        raise HTTPException(status_code=404, detail="session not found or not running")
    name = _container(sid)

    try:
        data = base64.b64decode(body.data_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="invalid base64")

    # Normalise the destination to live under /workspace; reject traversal.
    path = body.path or ""
    if not path.startswith("/"):
        path = f"{WORKSPACE}/{path}"
    if not _safe_under_workspace(path):
        raise HTTPException(status_code=400, detail="path must be under /workspace")

    parent = path.rsplit("/", 1)[0] or WORKSPACE
    _docker(["exec", name, "mkdir", "-p", parent], timeout=20)
    w = _docker(["exec", "-i", name, "sh", "-c", f"cat > {_shq(path)}"],
                input_bytes=data, timeout=60)
    if w.returncode != 0:
        raise HTTPException(status_code=500, detail=f"write failed: {w.stderr.decode(errors='replace')}")

    with _lock:
        _last_used[sid] = time.time()
    return {"ok": True}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    if not _valid_session(session_id):
        raise HTTPException(status_code=400, detail="invalid session_id")
    _docker(["rm", "-f", _container(session_id)], timeout=30)
    with _lock:
        _last_used.pop(session_id, None)
    return {"ok": True}


@app.get("/healthz")
def healthz():
    cp = _docker(["version", "-f", "{{.Server.Version}}"])
    ok = cp.returncode == 0
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"ok": ok, "docker": cp.stdout.decode(errors="replace").strip(), "image": IMAGE},
    )


# --- Output collection ------------------------------------------------------
def _snapshot_outputs(name: str) -> dict[str, str]:
    """Map of path -> "mtime|size" for every file under /workspace/outputs."""
    cp = _docker(
        ["exec", name, "sh", "-c",
         f"find {OUTPUTS_DIR} -type f -printf '%p\\t%T@\\t%s\\n' 2>/dev/null || true"],
        timeout=20,
    )
    snap: dict[str, str] = {}
    for line in cp.stdout.decode(errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            snap[parts[0]] = f"{parts[1]}|{parts[2]}"
    return snap


def _collect_new_files(name: str, before: dict[str, str]) -> list[dict]:
    after = _snapshot_outputs(name)
    changed = [p for p, meta in after.items() if before.get(p) != meta]
    changed.sort()
    files: list[dict] = []
    for path in changed:
        size = int(after[path].split("|")[1])
        if size > MAX_ARTIFACT_BYTES:
            continue  # §4.5: single artifact ≤ 20MB
        cp = _docker(["exec", name, "base64", "-w0", path], timeout=60)
        if cp.returncode != 0:
            continue
        b64 = cp.stdout.decode(errors="replace").strip()
        basename = path.rsplit("/", 1)[-1]
        mime = mimetypes.guess_type(basename)[0] or "application/octet-stream"
        files.append({"name": basename, "mime_type": mime, "data_base64": b64})
    return files


# --- Path safety ------------------------------------------------------------
def _safe_under_workspace(path: str) -> bool:
    # collapse and ensure it stays under /workspace (no .. escape)
    norm = os.path.normpath(path)
    return norm == WORKSPACE or norm.startswith(WORKSPACE + "/")


def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# --- Idle reaper ------------------------------------------------------------
def _reaper() -> None:
    while True:
        time.sleep(300)
        now = time.time()
        with _lock:
            stale = [sid for sid, t in _last_used.items() if now - t > IDLE_TTL_SECONDS]
        for sid in stale:
            _docker(["rm", "-f", _container(sid)], timeout=30)
            with _lock:
                _last_used.pop(sid, None)


@app.on_event("startup")
def _start_reaper() -> None:
    if PULL_ON_START:
        # Pull the runtime image up front (host daemon; not affected by the
        # per-session --network none). Don't crash the service if it fails.
        cp = _docker(["pull", IMAGE], timeout=600)
        if cp.returncode != 0:
            print(f"[sandbox] warning: failed to pull {IMAGE}: "
                  f"{cp.stderr.decode(errors='replace')[:200]}")
    threading.Thread(target=_reaper, daemon=True).start()


# --- Bearer-auth middleware -------------------------------------------------
@app.middleware("http")
async def _auth_mw(request, call_next):
    if API_KEY and request.url.path not in ("/healthz",):
        if request.headers.get("authorization") != f"Bearer {API_KEY}":
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return await call_next(request)
