"""Standard-library atomic record replacement, also used by the update worker."""

from __future__ import annotations

import errno
import os
import uuid
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path


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
