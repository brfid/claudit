"""Single-instance guard via an advisory lock on a PID file.

Prevents a second TUI from launching while one is already running —
e.g. an orphaned TUI from a closed terminal whose source files have
since moved, as happened with ai_cost_tracker.py.

Uses ``fcntl.flock`` (POSIX advisory), so the lock is released
automatically by the kernel when the process exits — even on ``kill
-9``. No stale-PID cleanup needed.
"""

from __future__ import annotations

import errno
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


class AlreadyRunning(RuntimeError):
    """Raised when another instance holds the lock."""

    def __init__(self, pid: Optional[int], path: Path):
        self.pid = pid
        self.path = path
        who = f"PID {pid}" if pid else "another process"
        super().__init__(
            f"claudit TUI is already running ({who}, lock at {path}). "
            f"Use --force to override."
        )


def default_pidfile_path() -> Path:
    """Return ``~/.cache/claudit/tui.pid`` (honors ``XDG_CACHE_HOME``)."""
    cache_home = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache_home) if cache_home else Path.home() / ".cache"
    return base / "claudit" / "tui.pid"


def _read_pid(path: Path) -> Optional[int]:
    """Best-effort PID read. Returns None on any error or empty file."""
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


@contextmanager
def single_instance(
    path: Optional[Path] = None, force: bool = False,
) -> Iterator[Path]:
    """Context manager that acquires an exclusive lock on ``path``.

    Yields the resolved pidfile path. Raises :class:`AlreadyRunning` if
    another process holds the lock, unless ``force=True``.

    On successful acquisition, writes the current PID into the file.
    Releases the lock (and truncates the file) on context exit.
    """
    path = (path or default_pidfile_path()).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Open read+write so we can read the holder's PID on contention
    # without truncating the file that the holder is using.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    flags = fcntl.LOCK_EX if force else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        fcntl.flock(fd, flags)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            raise AlreadyRunning(_read_pid(path), path) from None
        raise

    # Lock held. Write our PID for diagnostics.
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)

        yield path
    finally:
        # Truncate so a post-exit reader sees no stale PID. flock is
        # released implicitly by close(), but we unlock explicitly to
        # make intent clear and to tolerate unusual kernels.
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
