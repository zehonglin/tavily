"""Tests for the tavily key-pool gateway's selection/cooldown logic and the
client/gateway exit-code marker contract.

Run with:  pytest tests/
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "gateway"))

import tavily_gateway as gw  # noqa: E402


def _load_client():
    spec = importlib.util.spec_from_file_location("tvly_client", REPO / "client" / "tvly")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build(stdout: bytes, rc: int, marker: bytes) -> bytes:
    return stdout + b"\n" + marker + str(rc).encode() + b"\n"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Point file paths at a temp dir and reset in-memory state per test."""
    monkeypatch.setattr(gw, "KEYS_FILE", str(tmp_path / "keys.json"))
    monkeypatch.setattr(gw, "USAGE_CACHE", str(tmp_path / "usage_cache"))
    gw._cache.clear()
    gw._cooldown.clear()
    gw._disk_loaded = True  # skip on-disk bootstrap; tests drive _cache directly
    yield


def test_picks_most_remaining(monkeypatch):
    monkeypatch.setattr(gw, "_load_keys", lambda: ["A", "B", "C"])
    monkeypatch.setattr(gw, "_query_remaining", lambda k: {"A": 100, "B": 500, "C": 50}[k])
    assert gw.pick_best_key() == "B"


def test_cache_hit_skips_requery(monkeypatch):
    monkeypatch.setattr(gw, "_load_keys", lambda: ["A", "B", "C"])
    calls: list[str] = []

    def fake(k):
        calls.append(k)
        return {"A": 100, "B": 500, "C": 50}[k]

    monkeypatch.setattr(gw, "_query_remaining", fake)
    assert gw.pick_best_key() == "B"
    assert len(calls) == 3                  # queried each key once
    assert gw.pick_best_key() == "B"        # cache hit
    assert len(calls) == 3                  # no re-query within TTL


def test_expiry_triggers_requery(monkeypatch):
    monkeypatch.setattr(gw, "_load_keys", lambda: ["A", "B", "C"])
    monkeypatch.setattr(gw, "_query_remaining", lambda k: {"A": 100, "B": 500, "C": 50}[k])
    assert gw.pick_best_key() == "B"
    gw._cache["ts"] = int(time.time()) - gw.CACHE_TTL_SECONDS - 1  # force stale
    monkeypatch.setattr(gw, "_query_remaining", lambda k: {"A": 100, "B": 500, "C": 9999}[k])
    assert gw.pick_best_key() == "C"        # re-queried, C now max


def test_cooldown_skips_key(monkeypatch):
    monkeypatch.setattr(gw, "_load_keys", lambda: ["A", "B", "C"])
    monkeypatch.setattr(gw, "_query_remaining", lambda k: {"A": 100, "B": 500, "C": 50}[k])
    gw._cooldown["B"] = time.time() + 60    # B would win but is cooled
    assert gw.pick_best_key() == "A"        # next-best healthy key


def test_all_cooled_falls_back(monkeypatch):
    monkeypatch.setattr(gw, "_load_keys", lambda: ["A", "B"])
    monkeypatch.setattr(gw, "_query_remaining", lambda k: {"A": 10, "B": 90}[k])
    gw._cooldown["A"] = time.time() + 60
    gw._cooldown["B"] = time.time() + 60
    assert gw.pick_best_key() == "B"        # all cooled -> ignore cooldown


def test_maybe_cooldown_only_for_key_errors():
    gw._cooldown.clear()
    gw._maybe_cooldown("K", "Error: 401 Unauthorized")
    assert "K" in gw._cooldown
    gw._maybe_cooldown("K2", "network timeout, please retry")
    assert "K2" not in gw._cooldown


def test_exit_marker_roundtrip():
    tvly = _load_client()
    marker = tvly.EXIT_MARKER
    cases = [(b'{"r":[]}', 0), (b'', 1), (b'done\n', 0),
             (b'x' * 10000, 0), (b'', -9), (b'trail\n\n', 0)]
    for stdout, rc in cases:
        out, code = tvly._strip_exit_marker(_build(stdout, rc, marker))
        assert out == stdout, (stdout, rc, out)
        assert code == rc, (stdout, rc, code)
    # no marker -> truncated stream
    assert tvly._strip_exit_marker(b'partial output') == (b'partial output', None)
