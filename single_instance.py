"""Single-instance guard for bot.py.

Prevents the duplicate-bot situation seen in the wild (several ``bot.py``
processes fighting over the same Feishu long connection).  The guard is an
advisory file lock:

- POSIX uses ``fcntl.flock`` so the lock disappears automatically when the
  process dies, even after a crash.
- Windows falls back to an exclusive byte lock via ``msvcrt``.

The lock file doubles as ``logs/bot.pid`` so shell scripts can stop the bot.
"""
from __future__ import annotations

import os


class SingleInstanceError(RuntimeError):
    """Raised when another instance already holds the bot lock."""


class SingleInstance:
    def __init__(self, pid_file: str):
        self.pid_file = pid_file
        self._fd: int | None = None
        self.owner_pid: int | None = None

    def acquire(self) -> None:
        os.makedirs(os.path.dirname(self.pid_file) or ".", exist_ok=True)
        if os.name == "nt":
            self._acquire_windows()
        else:
            self._acquire_posix()

    def release(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            try:
                os.unlink(self.pid_file)
            except OSError:
                pass

    # ---------------- POSIX ----------------

    def _acquire_posix(self) -> None:
        import errno
        import fcntl

        fd = os.open(self.pid_file, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                self.owner_pid = self._read_existing_pid()
                raise SingleInstanceError(
                    f"已有 bot.py 实例在运行（PID {self.owner_pid or '未知'}）。"
                    "请先执行 scripts/stop.sh 停止旧实例，或手动结束旧进程后重试。"
                ) from exc
            raise
        self._fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.fsync(fd)

    # ---------------- Windows ----------------

    def _acquire_windows(self) -> None:
        import msvcrt

        fd = os.open(self.pid_file, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if os.path.getsize(self.pid_file) == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            self.owner_pid = self._read_existing_pid()
            raise SingleInstanceError(
                f"已有 bot.py 实例在运行（PID {self.owner_pid or '未知'}）。"
                "请先停止旧实例后重试。"
            ) from None
        self._fd = fd
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))

    def _read_existing_pid(self) -> int | None:
        try:
            with open(self.pid_file, "r", encoding="ascii") as fh:
                raw = fh.read().strip()
            return int(raw) if raw.isdigit() else None
        except OSError:
            return None


def acquire() -> SingleInstance:
    """Acquire the bot's single-instance lock or raise ``SingleInstanceError``."""
    project_root = os.path.dirname(os.path.abspath(__file__))
    guard = SingleInstance(os.path.join(project_root, "logs", "bot.pid"))
    guard.acquire()
    return guard
