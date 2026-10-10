"""Account-scoped leases for named nanobot stores shared by Python installations.

Runtimes share leases; backup/install maintenance requires exclusive access. Two
lease namespaces cover overlapping store roots in both directions. This coordinates
participating runtimes, not older versions, other accounts, external databases or
arbitrary project files.
Each process must acquire its own lease; a parent's exit must not unlock a pip child.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Generator, Iterable
from contextlib import ExitStack, contextmanager
from pathlib import Path

from nanobot.installation_lock import InstallationBusyError, file_access

_DATA_STORES = (
    "sessions", "webui", "media", "auth", "pairing.json", "cron",
    "llm_usage.sqlite3", "llm_usage.sqlite3-wal", "llm_usage.sqlite3-shm",
    "whatsapp-auth", "weixin", "matrix-store", "linear", "mochat", "cli-apps/installed.json",
)
_WORKSPACE_STORES = (
    ".nanobot", "memory", "skills", "sessions", "cron", "triggers",
    "AGENTS.md", "TOOLS.md", "SOUL.md", "USER.md", "HEARTBEAT.md",
)


def profile_data_paths(*, home: Path, data_dir: Path, config: Path, workspace: Path) -> tuple[Path, ...]:
    """One store inventory for runtime leases and scoped update backups.

    Keep store symlinks visible here: backup must reject them, not silently expand
    its read scope. Lease identity alone resolves aliases so runtimes cannot race.
    """
    return (
        config, home / "history" / "cli_history", home / "sessions",
        *(data_dir / name for name in _DATA_STORES),
        *(workspace / name for name in _WORKSPACE_STORES),
    )


@contextmanager
def data_access(paths: Iterable[str | Path], *, updating: bool = False) -> Generator[None]:
    """Refuse overlapping live writers/maintenance without waiting or killing them.

    Writers claim roots in one namespace and ancestors in the other; maintenance
    does the reverse, exclusively. Neither a parent nor a child store can bypass
    the other's lease. Disjoint runtime data stays available; maintenance jobs in
    this account serialize deliberately rather than require a live lock registry.
    Leases are nonblocking, ordered and released together after partial failure.
    This is not a durable handoff fence: callers must reacquire and verify their
    backup before mutation when moving the work to another process.
    """
    roots: set[Path] = set()
    for raw in paths:
        path = Path(raw)
        if not path.is_absolute():
            raise ValueError("Data stores must use absolute paths")
        roots.add(path.resolve())
    leases: set[tuple[str, str]] = set()
    for path in roots:
        requested, ancestors = (path,), (path, *path.parents)
        for namespace, items in (
            ("writers", requested if updating else ancestors),
            ("maintenance", ancestors if updating else requested),
        ):
            for item in items:
                # Conservatively merge case aliases, including on macOS volumes
                # where normcase alone does not normalize path identity.
                leases.add((namespace, str(item).casefold()))
    directory = Path.home() / ".nanobot" / "updates" / "data-locks"
    with ExitStack() as stack:
        try:
            for namespace, key in sorted(leases):
                name = hashlib.sha256(os.fsencode(key)).hexdigest()
                stack.enter_context(file_access(directory / f"{namespace}-{name}.lock", exclusive=updating))
        except BlockingIOError as exc:
            raise InstallationBusyError(
                "Nanobot data is in use by another runtime or update. Stop its writers "
                "before retrying; no data lease was granted."
            ) from exc
        yield
