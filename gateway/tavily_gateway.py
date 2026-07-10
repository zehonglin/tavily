#!/usr/bin/env python3
"""Tavily key-pool gateway.

Receives HTTP requests from thin clients (the `tvly` wrapper on agent machines),
picks the API key with the most remaining quota, spawns the real `tvly` CLI with
that key injected via TAVILY_API_KEY, and streams the CLI's stdout back to the
caller. CLI behavior is preserved bit-for-bit; the gateway only injects the key.

Endpoints
---------
POST /exec
    Body: {"cmd": "search", "args": ["query", "--json"], "stdin": "optional"}
    Headers: Authorization: Bearer <token>
    Response: streamed stdout of `tvly <cmd> <args...>`, followed by a trailing
    exit-code marker (see EXIT_MARKER) that the thin client strips to recover
    the CLI's real exit code. Non-zero exit is also logged server-side.

GET /healthz
    Liveness probe, no auth.

Key pool is read from $TAVILY_KEYS_FILE (default /etc/tavily/keys.json), a
JSON array of "tvly-..." strings. Usage is cached in $TAVILY_USAGE_CACHE
(default /var/lib/tavily/usage_cache) for TAVILY_CACHE_TTL seconds.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

KEYS_FILE = os.environ.get("TAVILY_KEYS_FILE", "/etc/tavily/keys.json")
USAGE_CACHE = os.environ.get("TAVILY_USAGE_CACHE", "/var/lib/tavily/usage_cache")
CACHE_TTL_SECONDS = int(os.environ.get("TAVILY_CACHE_TTL", "300"))
AUTH_TOKEN = os.environ.get("TAVILY_GATEWAY_TOKEN", "")
TVLY_BIN = os.environ.get("TVLY_BIN", "tvly")
LISTEN_HOST = os.environ.get("TAVILY_GATEWAY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("TAVILY_GATEWAY_PORT", "18790"))
TAVILY_USAGE_URL = "https://api.tavily.com/usage"

# Backstop limits so a flood of /exec or a hung CLI can't take the gateway down.
# MAX_CONCURRENT_EXECS bounds simultaneous tvly subprocesses; MAX_EXEC_SECONDS is
# the wall-clock kill deadline for a single exec (set >= client timeout).
MAX_CONCURRENT_EXECS = max(1, int(os.environ.get("TAVILY_MAX_CONCURRENT", "8")))
MAX_EXEC_SECONDS = int(os.environ.get("TAVILY_EXEC_TIMEOUT", "600"))

# A key whose CLI call fails with an auth/quota error is cooled down for this
# many seconds so subsequent calls skip it instead of failing the same way.
KEY_COOLDOWN_SECONDS = int(os.environ.get("TAVILY_KEY_COOLDOWN", "60"))

# Cap on POST /exec body size. stdin is only ever a short query string, so 10MB
# is far more than enough while preventing a bad client from exhausting memory.
MAX_BODY_BYTES = 10 * 1024 * 1024

# Trailing marker appended after the streamed stdout so the thin client can
# recover the CLI's real exit code (HTTP chunked trailers aren't readable via
# urllib). Full marker on the wire: b"\n" + EXIT_MARKER + "<code>" + b"\n".
EXIT_MARKER = b"__TVLY_GATEWAY_EXIT__:"

# In-memory key-pool cache. Cache hits take only _cache_lock (no I/O); refreshes
# run under _refresh_lock so a slow /usage query never blocks a fresh cache hit.
_cache: dict = {}
_cache_lock = threading.Lock()
_refresh_lock = threading.Lock()
_disk_loaded = False

# Per-key cooldown (epoch expiry) and the concurrency gate for spawned CLIs.
_cooldown: dict = {}
_cooldown_lock = threading.Lock()
_exec_sem = threading.BoundedSemaphore(MAX_CONCURRENT_EXECS)

# stderr fragments that indicate a failure is the key's fault (not the query),
# so we cool that key down instead of churning it again on the next call.
_BAD_KEY_TOKENS = (
    "401", "429", "unauthorized", "invalid api key", "invalid_api_key",
    "api key", "quota", "rate limit", "rate_limit", "exceeded", "insufficient",
)

# keys.json is hot-reloadable but only re-parsed when its mtime changes, so the
# fast path does a cheap stat instead of read+json.load on every request.
_keys_mtime: float = -1.0
_keys_cached: list[str] = []
_keys_cache_lock = threading.Lock()

# Structured logs go to stderr (systemd routes that to journald); a small set of
# in-memory counters is exposed at GET /metrics for monitoring/alerting.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_logger = logging.getLogger("tavily-gateway")

_metrics_lock = threading.Lock()
_metrics = {
    "exec_total": 0,
    "exec_errors": 0,
    "exec_in_flight": 0,
    "exec_seconds_sum": 0.0,
    "http_4xx": 0,
    "http_5xx": 0,
    "http_429": 0,
    "key_cooldowns": 0,
}


def _metric_inc(name: str, n: float = 1) -> None:
    with _metrics_lock:
        _metrics[name] = _metrics.get(name, 0) + n


def _load_keys() -> list[str]:
    """Return the configured keys, re-reading keys.json only when it changes."""
    global _keys_mtime, _keys_cached
    try:
        mtime = os.path.getmtime(KEYS_FILE)
    except OSError:
        with _keys_cache_lock:
            _keys_mtime, _keys_cached = -1.0, []
        return []
    with _keys_cache_lock:
        if mtime == _keys_mtime:
            return list(_keys_cached)
    try:
        with open(KEYS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        keys = [k for k in data if isinstance(k, str) and k] if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []  # don't poison the cache on a transient parse error
    with _keys_cache_lock:
        _keys_mtime, _keys_cached = mtime, keys
    return list(keys)


def _load_cache() -> dict:
    try:
        with open(USAGE_CACHE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    tmp = USAGE_CACHE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, USAGE_CACHE)
    except OSError:
        pass


def _query_remaining(key: str) -> int:
    """Return remaining quota for a key, or -1 if the lookup fails."""
    req = Request(TAVILY_USAGE_URL, headers={"Authorization": f"Bearer {key}"})
    try:
        with urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode("utf-8", "replace"))
        limit = d.get("account", {}).get("plan_limit", 1000)
        used = d.get("account", {}).get("plan_usage", 0)
        return int(limit) - int(used)
    except Exception:
        return -1


def _query_all_remaining(keys: list[str]) -> list[int]:
    """Remaining quota for every key, queried concurrently.

    Serially this is N * timeout worst case; in parallel the worst case is one
    timeout, so a flaky /usage can't stall callers for tens of seconds.
    """
    workers = max(1, min(16, len(keys)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_query_remaining, keys))


def _is_cooled(key: str, now: float) -> bool:
    with _cooldown_lock:
        return _cooldown.get(key, 0) > now


def _maybe_cooldown(key: str, stderr_text: str) -> None:
    """Cool a key down when its CLI failure looks key-related (auth/quota)."""
    low = stderr_text.lower()
    if not any(tok in low for tok in _BAD_KEY_TOKENS):
        return
    with _cooldown_lock:
        _cooldown[key] = time.time() + KEY_COOLDOWN_SECONDS
    _metric_inc("key_cooldowns")
    _logger.warning("cooled key …%s for %ds", key[-4:], KEY_COOLDOWN_SECONDS)


def pick_best_key() -> str:
    """Pick the key with the most remaining quota.

    Fresh, non-cooled cache hits are served from memory under a short lock with
    no I/O. Only a stale/missing/cooled cache triggers a refresh, and that runs
    under a separate lock with a double-check so concurrent callers don't all
    re-query /usage. Falls back to the first key if every lookup fails.
    """
    keys = _load_keys()
    if not keys:
        raise RuntimeError("no tavily keys configured in " + KEYS_FILE)

    # Load the persisted cache once so a fresh restart doesn't immediately
    # re-query every key.
    global _disk_loaded
    if not _disk_loaded:
        disk = _load_cache()
        if isinstance(disk, dict):
            with _cache_lock:
                if not _cache:
                    _cache.update(disk)
        _disk_loaded = True

    def fresh(c: dict, now: int) -> str | None:
        best = c.get("best_key")
        if best and best in keys and now - int(c.get("ts", 0)) < CACHE_TTL_SECONDS:
            return best
        return None

    now = int(time.time())
    with _cache_lock:
        hit = fresh(_cache, now)
    if hit and not _is_cooled(hit, time.time()):
        return hit

    # Stale or cooled: refresh under a dedicated lock. Other callers either wait
    # here or serve the freshly updated cache on the double-check.
    with _refresh_lock:
        now = int(time.time())
        with _cache_lock:
            hit = fresh(_cache, now)
        if hit and not _is_cooled(hit, time.time()):
            return hit

        remaining = _query_all_remaining(keys)
        # Prefer healthy keys; if every key is cooled, fall back to all of them.
        now_f = time.time()
        candidates = {k for k in keys if not _is_cooled(k, now_f)} or set(keys)
        best, best_remaining = "", -2
        for k, r in zip(keys, remaining):
            if k not in candidates:
                continue
            if r > best_remaining:
                best_remaining, best = r, k
        if not best:
            best = next(iter(candidates))

        ts = int(time.time())
        entry = {"best_key": best, "remaining": best_remaining, "ts": ts}
        with _cache_lock:
            _cache.clear()
            _cache.update(entry)
        _save_cache(entry)
        return best


class Handler(BaseHTTPRequestHandler):
    server_version = "tavily-gateway/1.0"

    def log_message(self, fmt, *args):  # quieter access logs
        pass

    def _send_json(self, code: int, payload: dict) -> None:
        if code == 429:
            _metric_inc("http_429")
        elif code >= 500:
            _metric_inc("http_5xx")
        elif code >= 400:
            _metric_inc("http_4xx")
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        if not AUTH_TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            # Constant-time compare to avoid leaking the token via timing.
            return secrets.compare_digest(header[len("Bearer "):].strip(), AUTH_TOKEN)
        return False

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(200, {"ok": True, "keys": len(_load_keys())})
            return
        if self.path in ("/metrics", "/logs") and not self._check_auth():
            self._send_json(401, {"error": "unauthorized"})
            return
        if self.path == "/metrics":
            with _metrics_lock:
                snap = dict(_metrics)
            lines = [
                "# HELP tavily_exec_total /exec requests accepted",
                "# TYPE tavily_exec_total counter",
                f"tavily_exec_total {snap['exec_total']}",
                "# HELP tavily_exec_errors /exec whose CLI exited non-zero",
                "# TYPE tavily_exec_errors counter",
                f"tavily_exec_errors {snap['exec_errors']}",
                "# HELP tavily_exec_in_flight Currently running tvly subprocesses",
                "# TYPE tavily_exec_in_flight gauge",
                f"tavily_exec_in_flight {snap['exec_in_flight']}",
                "# HELP tavily_exec_seconds_sum Total wall-clock seconds spent in /exec",
                "# TYPE tavily_exec_seconds_sum counter",
                f"tavily_exec_seconds_sum {snap['exec_seconds_sum']}",
                "# HELP tavily_http_4xx_total 4xx responses (excl. 429)",
                "# TYPE tavily_http_4xx_total counter",
                f"tavily_http_4xx_total {snap['http_4xx']}",
                "# HELP tavily_http_5xx_total 5xx responses",
                "# TYPE tavily_http_5xx_total counter",
                f"tavily_http_5xx_total {snap['http_5xx']}",
                "# HELP tavily_http_429_total 429 busy responses",
                "# TYPE tavily_http_429_total counter",
                f"tavily_http_429_total {snap['http_429']}",
                "# HELP tavily_key_cooldowns Keys put into cooldown",
                "# TYPE tavily_key_cooldowns counter",
                f"tavily_key_cooldowns {snap['key_cooldowns']}",
                "",
            ]
            body = "\n".join(lines).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/logs":
            with _log_lock:
                recent = list(_log_lines[-200:])
            body = ("\n".join(recent) + "\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/exec":
            self._send_json(404, {"error": "not found"})
            return
        if not self._check_auth():
            self._send_json(401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "invalid content-length"})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "request body too large"})
            return
        try:
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "invalid json body"})
            return

        cmd = payload.get("cmd")
        args = payload.get("args", [])
        stdin_text = payload.get("stdin")

        if not isinstance(cmd, str) or not cmd:
            self._send_json(400, {"error": "missing 'cmd'"})
            return
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            self._send_json(400, {"error": "'args' must be a list of strings"})
            return

        # Reject obviously dangerous command names; the CLI surface is known.
        if not cmd.replace("-", "").replace("_", "").isalnum():
            self._send_json(400, {"error": "invalid command name"})
            return

        try:
            api_key = pick_best_key()
        except RuntimeError as exc:
            self._send_json(503, {"error": str(exc)})
            return

        if shutil.which(TVLY_BIN) is None:
            self._send_json(500, {"error": f"{TVLY_BIN!r} not found on gateway host"})
            return

        env = os.environ.copy()
        env["TAVILY_API_KEY"] = api_key
        stdin_bytes = stdin_text.encode("utf-8") if isinstance(stdin_text, str) else None

        # Bound simultaneous tvly subprocesses; fail fast (429) instead of
        # piling up processes under load.
        if not _exec_sem.acquire(blocking=False):
            self._send_json(429, {"error": "gateway busy, retry later"})
            return

        start = time.monotonic()
        _metric_inc("exec_total")
        _metric_inc("exec_in_flight")
        exec_done = threading.Event()
        watchdog_thread: threading.Thread | None = None
        try:
            # Stream CLI stdout straight through. research/poll can run for
            # minutes, so we must not buffer the whole output nor apply a tight
            # timeout.
            try:
                proc = subprocess.Popen(
                    [TVLY_BIN, cmd, *args],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=(subprocess.PIPE if stdin_bytes is not None else None),
                    env=env,
                    text=False,
                    bufsize=0,
                )
            except OSError as exc:
                _logger.warning("spawn failed for %s: %s", cmd, exc)
                self._send_json(500, {"error": f"failed to spawn {TVLY_BIN}: {exc}"})
                return

            if stdin_bytes is not None:
                try:
                    proc.stdin.write(stdin_bytes)
                    proc.stdin.close()
                except BrokenPipeError:
                    pass

            # Drain stderr on a background thread. If we only read stderr after
            # the process exits, a verbose CLI can fill the OS pipe buffer
            # (~64KB), block on the write, stop producing stdout, and deadlock
            # our read loop.
            stderr_buf: list[bytes] = []
            stderr_size = 0

            def _drain_stderr() -> None:
                nonlocal stderr_size
                stream = proc.stderr
                if stream is None:
                    return
                try:
                    while True:
                        chunk = stream.read(4096)
                        if not chunk:
                            break
                        stderr_buf.append(chunk)
                        stderr_size += len(chunk)
                        # Keep only the tail so a chatty CLI can't grow this unbounded.
                        while stderr_size > 8192 and len(stderr_buf) > 1:
                            stderr_size -= len(stderr_buf.pop(0))
                except (OSError, ValueError):
                    pass

            stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
            stderr_thread.start()

            # Wall-clock watchdog: if the CLI hangs without exiting (and the
            # client hasn't disconnected to unblock us), kill it at the deadline.
            def _watchdog() -> None:
                if not exec_done.wait(timeout=MAX_EXEC_SECONDS):
                    proc.kill()

            watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
            watchdog_thread.start()

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            aborted = False
            try:
                for chunk in iter(lambda: proc.stdout.read(4096), b""):
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                aborted = True
                proc.kill()

            # stdout is drained (or the client vanished). Wait for exit so we can
            # append an exit-code marker the thin client strips to recover the
            # real return code — without it, a failed CLI looks like success.
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

            if not aborted:
                rc = proc.returncode if proc.returncode is not None else 0
                try:
                    marker = b"\n" + EXIT_MARKER + str(rc).encode("ascii") + b"\n"
                    self.wfile.write(f"{len(marker):X}\r\n".encode("ascii") + marker + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            stderr_thread.join(timeout=5)
            dt = time.monotonic() - start
            _metric_inc("exec_seconds_sum", dt)
            if proc.returncode and proc.returncode != 0:
                stderr_tail = b"".join(stderr_buf).decode("utf-8", "replace")[-2000:]
                _maybe_cooldown(api_key, stderr_tail)
                _metric_inc("exec_errors")
                _log(f"[exit {proc.returncode}] tvly {cmd} {' '.join(args)}\n{stderr_tail}")
            _logger.info("exec cmd=%s exit=%s dt=%.2fs", cmd, proc.returncode, dt)
        finally:
            _metric_inc("exec_in_flight", -1)
            exec_done.set()
            if watchdog_thread is not None:
                watchdog_thread.join(timeout=1)
            _exec_sem.release()


_log_lines: list[str] = []
_log_lock = threading.Lock()


def _log(line: str) -> None:
    _logger.error(line)
    with _log_lock:
        _log_lines.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}")
        if len(_log_lines) > 2000:
            del _log_lines[:1000]


def main() -> None:
    if not AUTH_TOKEN:
        print("WARNING: TAVILY_GATEWAY_TOKEN not set - running WITHOUT auth")
    keys = _load_keys()
    print(f"tavily-gateway listening on {LISTEN_HOST}:{LISTEN_PORT}, {len(keys)} keys, tvly={TVLY_BIN}")
    if not keys:
        print(f"WARNING: no keys found in {KEYS_FILE}")
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)

    # Graceful shutdown: on SIGTERM/SIGINT, stop accepting new connections from a
    # separate thread (shutdown() must not run on the serve_forever thread) and
    # let serve_forever return. In-flight request threads are daemons, so the
    # process exits once the listener is down.
    def _stop(_signum, _frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _stop)
    # SIGTERM is POSIX-only; signal.signal raises on Windows for it, so guard.
    for _sig in (getattr(signal, "SIGTERM", None),):
        if _sig is not None:
            try:
                signal.signal(_sig, _stop)
            except (ValueError, OSError, RuntimeError):
                pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()