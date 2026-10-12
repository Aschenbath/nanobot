"""Standard-library atomic record replacement, also used by the update worker."""

from __future__ import annotations

import errno
import os
import uuid
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO


def _open_snapshot(path: Path) -> BinaryIO:
    """Read one inode without preventing its atomic replacement on Windows."""
    if os.name != "nt":
        return path.open("rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # GENERIC_READ; FILE_SHARE_READ | WRITE | DELETE; OPEN_EXISTING.
    # Python's normal open omits SHARE_DELETE, so even a short status poll can
    # make a concurrent os.replace fail. Readers must not block journal commits.
    handle = create(str(path), 0x80000000, 1 | 2 | 4, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    # open_osfhandle transfers handle ownership to the file descriptor.
    return os.fdopen(descriptor, "rb")


def read_atomic_bytes(path: Path, *, max_bytes: int = -1) -> bytes:
    """Read a snapshot while permitting writers to atomically publish the next."""
    with _open_snapshot(path) as stream:
        return stream.read(max_bytes)


def atomic_write_lines(path: Path, lines: Iterable[str], *, fsync: bool = True) -> None:
    """Replace serialized records, optionally syncing both file and directory.

    A unique sibling is flushed before replacement and cleaned on every failure.
    Directory fsync tolerates Windows permission errors and unsupported filesystems,
    but propagates other I/O failures. Existing callers retain their durability choice.
    """
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        # Stable bytes across platforms: callers hash records and bound their
        # encoded size, so Windows newline translation would break the contract.
        with open(tmp, "x", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                handle.write(line if line.endswith("\n") else f"{line}\n")
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(tmp, path)
        if fsync:
            with suppress(PermissionError):
                fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    try:
                        os.fsync(fd)
                    except OSError as exc:
                        if exc.errno != errno.EINVAL:
                            raise
                finally:
                    os.close(fd)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
