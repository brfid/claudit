"""Tests for the single-instance guard.

Exercises:
    * fresh acquire → succeeds and writes PID
    * second acquire in a subprocess → raises AlreadyRunning with the
      holder's PID
    * release → lock is available again and file is truncated
    * ``force=True`` → acquires even while another process holds it
    * default path honors ``XDG_CACHE_HOME``
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claudit.pidfile import (
    AlreadyRunning,
    default_pidfile_path,
    single_instance,
)


# ── Helpers ────────────────────────────────────────────────────────────

# Small Python program used as a lock-holder subprocess. Acquires the
# lock at ``sys.argv[1]``, prints ``READY`` + its own PID to stdout,
# then sleeps until stdin closes so the parent controls its lifetime.
_HOLDER_SRC = """
import sys
from pathlib import Path
from claudit.pidfile import single_instance

path = Path(sys.argv[1])
with single_instance(path=path):
    print("READY", flush=True)
    # Block until parent closes our stdin (EOF on read)
    sys.stdin.read()
"""


def _spawn_holder(path: Path) -> subprocess.Popen:
    """Start a child that holds the lock on ``path`` until its stdin closes."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SRC, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Wait for READY — indicates the child has the lock.
    assert proc.stdout is not None
    line = proc.stdout.readline()
    assert line.startswith("READY"), (
        f"holder did not signal ready: stdout={line!r} "
        f"stderr={proc.stderr.read() if proc.stderr else ''!r}"
    )
    return proc


def _stop_holder(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    """Close stdin so the holder exits cleanly, then wait."""
    if proc.stdin:
        proc.stdin.close()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ── Tests ──────────────────────────────────────────────────────────────

def test_fresh_acquire_writes_pid(tmp_path: Path) -> None:
    pid_path = tmp_path / "tui.pid"
    with single_instance(path=pid_path) as held:
        assert held == pid_path
        assert pid_path.exists()
        assert pid_path.read_text().strip() == str(os.getpid())


def test_release_truncates_file(tmp_path: Path) -> None:
    pid_path = tmp_path / "tui.pid"
    with single_instance(path=pid_path):
        pass
    # File still exists but is empty after release.
    assert pid_path.exists()
    assert pid_path.read_text() == ""


def test_reacquire_after_release(tmp_path: Path) -> None:
    pid_path = tmp_path / "tui.pid"
    with single_instance(path=pid_path):
        pass
    # Second acquisition in the same process must succeed.
    with single_instance(path=pid_path):
        assert pid_path.read_text().strip() == str(os.getpid())


def test_contention_raises_with_holder_pid(tmp_path: Path) -> None:
    pid_path = tmp_path / "tui.pid"
    holder = _spawn_holder(pid_path)
    try:
        with pytest.raises(AlreadyRunning) as excinfo:
            with single_instance(path=pid_path):
                pytest.fail("should not acquire while holder is alive")
        assert excinfo.value.pid == holder.pid
        assert excinfo.value.path == pid_path
    finally:
        _stop_holder(holder)


def test_force_acquires_after_holder_exits(tmp_path: Path) -> None:
    """``force=True`` uses a blocking acquire; once the holder exits it wins.

    We schedule the holder to exit shortly after we start blocking, so the
    blocking acquire completes in bounded time without flake risk.
    """
    pid_path = tmp_path / "tui.pid"
    holder = _spawn_holder(pid_path)

    # Schedule holder shutdown on a background timer.
    import threading
    threading.Timer(0.3, lambda: _stop_holder(holder)).start()

    start = time.monotonic()
    with single_instance(path=pid_path, force=True):
        # We got the lock — must have waited for the holder to exit.
        elapsed = time.monotonic() - start
        assert elapsed >= 0.2, f"force acquired too fast ({elapsed:.3f}s)"
        assert pid_path.read_text().strip() == str(os.getpid())


def test_contention_exception_message_includes_pid(tmp_path: Path) -> None:
    pid_path = tmp_path / "tui.pid"
    holder = _spawn_holder(pid_path)
    try:
        with pytest.raises(AlreadyRunning) as excinfo:
            with single_instance(path=pid_path):
                pass
        msg = str(excinfo.value)
        assert str(holder.pid) in msg
        assert "--force" in msg
    finally:
        _stop_holder(holder)


def test_default_path_honors_xdg_cache_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert default_pidfile_path() == tmp_path / "claudit" / "tui.pid"


def test_default_path_falls_back_to_home_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() reads HOME on POSIX.
    assert default_pidfile_path() == tmp_path / ".cache" / "claudit" / "tui.pid"


def test_creates_parent_directory(tmp_path: Path) -> None:
    pid_path = tmp_path / "nested" / "deeper" / "tui.pid"
    assert not pid_path.parent.exists()
    with single_instance(path=pid_path):
        assert pid_path.parent.is_dir()
