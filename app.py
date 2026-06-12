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
  - memory / cpu / pids / nofile limits
  - 120s exec timeout (overridable per-call, capped)
  - stdout/stderr streamed through a 32KB cap before returning to the model
  - artifacts capped per file, per exec count, and per exec total bytes
  - 30min idle TTL reaper

This is a DEV / single-host tool. For production swap `docker run` here for a
gVisor / microVM / E2B backend — the Go side and this HTTP contract do not move.
"""

from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from dataclasses import dataclass
import mimetypes
import os
import re
import secrets
import selectors
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
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
MAX_SESSIONS = int(os.environ.get("SANDBOX_MAX_SESSIONS", "16"))
MAX_CONCURRENT_EXECS = int(os.environ.get("SANDBOX_MAX_CONCURRENT_EXECS", "4"))
MAX_CONCURRENT_CREATES = int(os.environ.get("SANDBOX_MAX_CONCURRENT_CREATES", "2"))
QUEUE_TIMEOUT_SECONDS = float(os.environ.get("SANDBOX_QUEUE_TIMEOUT_SECONDS", "150"))
# When truthy, `docker pull` the runtime image once on startup so a fresh
# server doesn't fail the first /sessions call. Best-effort: logs and continues.
PULL_ON_START = os.environ.get("SANDBOX_PULL_ON_START", "") not in ("", "0", "false")
READ_ONLY_ROOTFS = os.environ.get("SANDBOX_READ_ONLY_ROOTFS", "") not in ("", "0", "false")
TMPFS_SIZE = os.environ.get("SANDBOX_TMPFS_SIZE", "256m")
WORKSPACE_TMPFS_SIZE = os.environ.get("SANDBOX_WORKSPACE_TMPFS_SIZE", "512m")
NOFILE_ULIMIT = os.environ.get("SANDBOX_NOFILE_ULIMIT", "1024:1024")

MAX_OUTPUT_BYTES = 32 * 1024          # stdout/stderr truncation
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024  # single produced file cap
MAX_UPLOAD_BYTES = int(os.environ.get("SANDBOX_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
MAX_FILES_PER_EXEC = int(os.environ.get("SANDBOX_MAX_FILES_PER_EXEC", "20"))
MAX_TOTAL_ARTIFACT_BYTES = int(os.environ.get("SANDBOX_MAX_TOTAL_ARTIFACT_BYTES", str(50 * 1024 * 1024)))

CONTAINER_PREFIX = "aurelia-sbx-"
LABEL = "aurelia.sandbox=1"
WORKSPACE = "/workspace"
OUTPUTS_DIR = f"{WORKSPACE}/outputs"
UPLOADS_DIR = f"{WORKSPACE}/uploads"

# session_id -> last-used epoch seconds (for the idle reaper). Container state
# itself lives in Docker, so a sidecar restart only loses TTL tracking, not
# sessions.
_last_used: dict[str, float] = {}


@dataclass
class _SessionState:
    lock: threading.Lock
    refs: int = 0


_session_states: dict[str, _SessionState] = {}
_state_lock = threading.Lock()
_exec_slots = threading.BoundedSemaphore(MAX_CONCURRENT_EXECS) if MAX_CONCURRENT_EXECS > 0 else None
_create_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CREATES) if MAX_CONCURRENT_CREATES > 0 else None

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


@dataclass
class _BoundedOutput:
    limit: int = MAX_OUTPUT_BYTES
    data: bytes = b""
    truncated: bool = False

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        remaining = self.limit - len(self.data)
        if remaining > 0:
            self.data += chunk[:remaining]
        if len(chunk) > remaining:
            self.truncated = True

    def text(self, label: str) -> str:
        data = self.data
        if self.truncated:
            data += f"\n... [truncated, {label} exceeded {self.limit // 1024}KB]".encode()
        return data.decode("utf-8", errors="replace")


@contextmanager
def _slot(sem: Optional[threading.BoundedSemaphore], what: str) -> Iterator[None]:
    if sem is None:
        yield
        return
    if not sem.acquire(timeout=QUEUE_TIMEOUT_SECONDS):
        raise HTTPException(status_code=429, detail=f"{what} queue is full")
    try:
        yield
    finally:
        sem.release()


@contextmanager
def _session_lock(session_id: str) -> Iterator[None]:
    with _state_lock:
        state = _session_states.setdefault(session_id, _SessionState(threading.Lock()))
        state.refs += 1
    if not state.lock.acquire(timeout=QUEUE_TIMEOUT_SECONDS):
        with _state_lock:
            state.refs -= 1
            if state.refs == 0 and session_id not in _last_used:
                _session_states.pop(session_id, None)
        raise HTTPException(status_code=429, detail="session is busy")
    try:
        yield
    finally:
        state.lock.release()
        with _state_lock:
            state.refs -= 1
            if state.refs == 0 and session_id not in _last_used:
                _session_states.pop(session_id, None)


def _touch(session_id: str) -> None:
    with _state_lock:
        _last_used[session_id] = time.time()
        _session_states.setdefault(session_id, _SessionState(threading.Lock()))


def _forget(session_id: str) -> None:
    with _state_lock:
        _last_used.pop(session_id, None)
        state = _session_states.get(session_id)
        if state is not None and state.refs == 0:
            _session_states.pop(session_id, None)


def _count_live_sessions() -> int:
    cp = _docker(["ps", "-q", "--filter", f"label={LABEL}"], timeout=20)
    if cp.returncode != 0:
        return len(_last_used)
    return len([line for line in cp.stdout.splitlines() if line.strip()])


def _timeout_arg(seconds: float) -> str:
    return f"{max(seconds, 1.0):.3f}s"


def _discover_sessions() -> None:
    cp = _docker(
        [
            "ps", "-a",
            "--filter", f"label={LABEL}",
            "--format", "{{.Names}}\t{{.State}}\t{{.CreatedAt}}",
        ],
        timeout=30,
    )
    if cp.returncode != 0:
        print(f"[sandbox] warning: failed to discover existing sessions: "
              f"{cp.stderr.decode(errors='replace')[:200]}")
        return

    now = time.time()
    for line in cp.stdout.decode(errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name, state = parts[0], parts[1]
        if not name.startswith(CONTAINER_PREFIX):
            continue
        sid = name[len(CONTAINER_PREFIX):]
        if not _valid_session(sid):
            continue
        if state.lower() != "running":
            _docker(["rm", "-f", name], timeout=30)
            continue
        _touch(sid)
    with _state_lock:
        recovered = len(_last_used)
    if recovered:
        print(f"[sandbox] recovered {recovered} existing session container(s)")


def _run_exec_bounded(name: str, cmd: list[str], *, timeout: float) -> tuple[int, str, str]:
    stdout = _BoundedOutput()
    stderr = _BoundedOutput()
    start = time.monotonic()
    proc = subprocess.Popen(
        ["docker", "exec", name, *cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None

    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, stdout)
    sel.register(proc.stderr, selectors.EVENT_READ, stderr)
    timed_out = False

    try:
        while sel.get_map():
            if time.monotonic() - start > timeout:
                timed_out = True
                proc.kill()
                break
            for key, _ in sel.select(timeout=0.2):
                chunk = key.fileobj.read1(8192)
                if chunk:
                    key.data.append(chunk)
                else:
                    sel.unregister(key.fileobj)
                    key.fileobj.close()
        exit_code = proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        exit_code = 124
    finally:
        for key in list(sel.get_map().values()):
            try:
                sel.unregister(key.fileobj)
            except Exception:
                pass
            try:
                key.fileobj.close()
            except Exception:
                pass

    if timed_out:
        exit_code = 124
    return exit_code, stdout.text("stdout"), stderr.text("stderr")


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
    with _slot(_create_slots, "session create"):
        if MAX_SESSIONS > 0 and _count_live_sessions() >= MAX_SESSIONS:
            raise HTTPException(status_code=429, detail="too many active sessions")

        session_id = uuid.uuid4().hex
        name = _container(session_id)
        args = [
            "run", "-d",
            "--name", name,
            "--label", LABEL,
            "--label", f"aurelia.session_id={session_id}",
            "--label", f"aurelia.created_at={int(time.time())}",
            "--network", NETWORK,
            "--memory", MEMORY,
            "--memory-swap", MEMORY,
            "--cpus", CPUS,
            "--pids-limit", PIDS_LIMIT,
            "--ulimit", f"nofile={NOFILE_ULIMIT}",
            "--init",
            "--user", "1000:1000",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "-w", WORKSPACE,
        ]
        if READ_ONLY_ROOTFS:
            args.extend([
                "--read-only",
                "--tmpfs", f"/tmp:rw,nosuid,nodev,size={TMPFS_SIZE}",
                "--tmpfs", f"/home/sandbox:rw,nosuid,nodev,size={TMPFS_SIZE}",
                "--tmpfs", f"{WORKSPACE}:rw,nosuid,nodev,size={WORKSPACE_TMPFS_SIZE}",
            ])
        args.extend([IMAGE, "sleep", "infinity"])
        cp = _docker(args, timeout=60)
        if cp.returncode != 0:
            raise HTTPException(status_code=500, detail=f"docker run failed: {cp.stderr.decode(errors='replace')}")
        # Make sure the standard dirs exist (image already creates them, but be safe).
        mk = _docker(["exec", name, "mkdir", "-p", UPLOADS_DIR, OUTPUTS_DIR], timeout=20)
        if mk.returncode != 0:
            _docker(["rm", "-f", name], timeout=30)
            raise HTTPException(status_code=500, detail=f"workspace init failed: {mk.stderr.decode(errors='replace')}")
        _touch(session_id)
        return {"session_id": session_id}


@app.post("/exec")
def exec_code(body: ExecBody):
    sid = body.session_id
    if not _valid_session(sid):
        raise HTTPException(status_code=400, detail="invalid session_id")
    name = _container(sid)

    timeout_ms = body.timeout_ms or EXEC_TIMEOUT_CAP_MS
    timeout_ms = max(1000, min(timeout_ms, EXEC_TIMEOUT_CAP_MS))
    timeout_s = timeout_ms / 1000.0

    with _session_lock(sid), _slot(_exec_slots, "exec"):
        if not _is_running(sid):
            raise HTTPException(status_code=404, detail="session not found or not running")
        cell_path = f"/tmp/aurelia-cell-{uuid.uuid4().hex}.py"

        # Write the cell to a file in the container (stdin avoids arg-length
        # limits and shell-quoting hazards).
        w = _docker(["exec", "-i", name, "sh", "-c", f"cat > {_shq(cell_path)}"],
                    input_bytes=body.code.encode("utf-8"), timeout=30)
        if w.returncode != 0:
            raise HTTPException(status_code=500, detail=f"write cell failed: {w.stderr.decode(errors='replace')}")

        before = _snapshot_outputs(name)

        # `timeout` kills runaway code inside the container; the host-side
        # reader keeps stdout/stderr bounded so the sidecar cannot be OOMed by
        # a print loop.
        try:
            exit_code, stdout, stderr = _run_exec_bounded(
                name,
                ["timeout", "--kill-after=2s", _timeout_arg(timeout_s), "python", cell_path],
                timeout=timeout_s + 15,
            )
        finally:
            _docker(["exec", name, "rm", "-f", cell_path], timeout=10)

        if exit_code == 124:  # `timeout` convention
            stderr = stderr + f"\n[sandbox] execution exceeded {timeout_s:.3f}s and was killed"

        _touch(sid)

        files, artifact_warning = _collect_new_files(name, before)
        if artifact_warning:
            stderr = (stderr + "\n" + artifact_warning).strip()
        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "files": files,
        }


@app.post("/files")
def put_file(body: FilesBody):
    sid = body.session_id
    if not _valid_session(sid):
        raise HTTPException(status_code=400, detail="invalid session_id")
    name = _container(sid)

    max_b64_len = ((MAX_UPLOAD_BYTES + 2) // 3) * 4
    if len(body.data_base64) > max_b64_len:
        raise HTTPException(status_code=413, detail="file too large")
    try:
        data = base64.b64decode(body.data_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="invalid base64")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large")

    # Normalise the destination to live under /workspace; reject traversal.
    path = body.path or ""
    if not path.startswith("/"):
        path = f"{WORKSPACE}/{path}"
    if not _safe_under_workspace(path):
        raise HTTPException(status_code=400, detail="path must be under /workspace")

    with _session_lock(sid):
        if not _is_running(sid):
            raise HTTPException(status_code=404, detail="session not found or not running")
        parent = path.rsplit("/", 1)[0] or WORKSPACE
        mk = _docker(["exec", name, "mkdir", "-p", parent], timeout=20)
        if mk.returncode != 0:
            raise HTTPException(status_code=500, detail=f"mkdir failed: {mk.stderr.decode(errors='replace')}")
        w = _docker(["exec", "-i", name, "sh", "-c", f"cat > {_shq(path)}"],
                    input_bytes=data, timeout=60)
        if w.returncode != 0:
            raise HTTPException(status_code=500, detail=f"write failed: {w.stderr.decode(errors='replace')}")

        _touch(sid)
        return {"ok": True}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    if not _valid_session(session_id):
        raise HTTPException(status_code=400, detail="invalid session_id")
    with _session_lock(session_id):
        _docker(["rm", "-f", _container(session_id)], timeout=30)
        _forget(session_id)
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


def _collect_new_files(name: str, before: dict[str, str]) -> tuple[list[dict], str]:
    after = _snapshot_outputs(name)
    changed = [p for p, meta in after.items() if before.get(p) != meta]
    changed.sort()
    files: list[dict] = []
    total_bytes = 0
    skipped = 0
    for path in changed:
        size = int(after[path].split("|")[1])
        if size > MAX_ARTIFACT_BYTES:
            skipped += 1
            continue  # §4.5: single artifact ≤ 20MB
        if len(files) >= MAX_FILES_PER_EXEC:
            skipped += 1
            continue
        if total_bytes + size > MAX_TOTAL_ARTIFACT_BYTES:
            skipped += 1
            continue  # §4.5: single artifact ≤ 20MB
        cp = _docker(["exec", name, "base64", "-w0", path], timeout=60)
        if cp.returncode != 0:
            skipped += 1
            continue
        b64 = cp.stdout.decode(errors="replace").strip()
        basename = path.rsplit("/", 1)[-1]
        mime = mimetypes.guess_type(basename)[0] or "application/octet-stream"
        files.append({"name": basename, "mime_type": mime, "data_base64": b64})
        total_bytes += size
    warning = ""
    if skipped:
        warning = (
            f"[sandbox] skipped {skipped} output file(s) because artifact limits "
            f"are {MAX_FILES_PER_EXEC} files, {MAX_ARTIFACT_BYTES} bytes per file, "
            f"{MAX_TOTAL_ARTIFACT_BYTES} bytes total"
        )
    return files, warning


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
        with _state_lock:
            stale = [sid for sid, t in _last_used.items() if now - t > IDLE_TTL_SECONDS]
        for sid in stale:
            try:
                with _session_lock(sid):
                    _docker(["rm", "-f", _container(sid)], timeout=30)
                    _forget(sid)
            except HTTPException:
                print(f"[sandbox] warning: session {sid} stayed busy during reap")


@app.on_event("startup")
def _start_reaper() -> None:
    if PULL_ON_START:
        # Pull the runtime image up front (host daemon; not affected by the
        # per-session --network none). Don't crash the service if it fails.
        cp = _docker(["pull", IMAGE], timeout=600)
        if cp.returncode != 0:
            print(f"[sandbox] warning: failed to pull {IMAGE}: "
                  f"{cp.stderr.decode(errors='replace')[:200]}")
    _discover_sessions()
    threading.Thread(target=_reaper, daemon=True).start()


# --- Bearer-auth middleware -------------------------------------------------
@app.middleware("http")
async def _auth_mw(request, call_next):
    if API_KEY and request.url.path not in ("/healthz",):
        if not secrets.compare_digest(request.headers.get("authorization", ""), f"Bearer {API_KEY}"):
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return await call_next(request)
