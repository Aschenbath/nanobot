"""Account-scoped leases for named nanobot stores shared by Python installations.

Runtimes share leases; backup/install maintenance requires exclusive access. Two
lease namespaces cover overlapping store roots in both directions. This coordinates
participating runtimes, not older versions, other accounts, external databases or
arbitrary project files.
Each process must acquire its own lease; a parent's exit must not unlock a pip child.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Generator, Iterable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID

from nanobot.installation_lock import InstallationBusyError, file_access
from nanobot.utils.atomic_file import atomic_write_lines

_DATA_STORES = (
    "sessions", "webui", "media", "auth", "pairing.json", "cron",
    "llm_usage.sqlite3", "llm_usage.sqlite3-wal", "llm_usage.sqlite3-shm",
    "whatsapp-auth", "weixin", "matrix-store", "linear", "mochat", "cli-apps/installed.json",
)
_WORKSPACE_STORES = (
    ".nanobot", "memory", "skills", "sessions", "cron", "triggers",
    "AGENTS.md", "TOOLS.md", "SOUL.md", "USER.md", "HEARTBEAT.md",
)
_MAX_RESERVATION_BYTES = 16 * 1024 * 1024


def _reservation(path: Path) -> tuple[str, tuple[str, ...]] | None:
    """Read a bounded, atomically replaced reservation; corruption never means idle."""
    if path.is_symlink():
        raise ValueError("Invalid data maintenance reservation")
    try:
        with path.open("rb") as stream:
            encoded = stream.read(_MAX_RESERVATION_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(encoded) > _MAX_RESERVATION_BYTES:
        raise ValueError("Data maintenance reservation is too large")
    raw: object = json.loads(encoded)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("Invalid data maintenance reservation")
    record = cast(dict[str, object], raw)
    owner, roots = record.get("job_id"), record.get("roots")
    if (record.get("schema_version") != 1 or not isinstance(owner, str)
            or str(UUID(owner)) != owner or not isinstance(roots, list)):
        raise ValueError("Invalid data maintenance reservation")
    values = cast(list[object], roots)
    if not 1 <= len(values) <= 40_000 or any(
        not isinstance(root, str) or len(root) > 4096 or "\0" in root or not Path(root).is_absolute()
        for root in values
    ):
        raise ValueError("Invalid data maintenance scope")
    return owner, tuple(cast(list[str], values))


@dataclass(frozen=True)
class DataLease:
    """Reservation operations are valid only inside the owning data_access context."""

    roots: tuple[str, ...]
    reservation_path: Path
    job_id: str | None

    def reserve(self) -> None:
        """Keep participating writers out through handoff, crash and repair."""
        if self.job_id is None:
            raise ValueError("Only an update job may reserve data maintenance")
        encoded = json.dumps({"schema_version": 1, "job_id": self.job_id, "roots": self.roots})
        if len(encoded.encode("utf-8")) + 1 > _MAX_RESERVATION_BYTES:
            raise ValueError("Data maintenance reservation is too large")
        atomic_write_lines(self.reservation_path, [encoded])
        self.reservation_path.chmod(0o600)

    def release_reservation(self) -> None:
        """Clear only this job's reservation after verification or safe cancellation."""
        if self.job_id is None:
            raise ValueError("Only an update job may release data maintenance")
        current = _reservation(self.reservation_path)
        if current is None:
            return
        if current != (self.job_id, self.roots):
            raise ValueError("Data maintenance belongs to a different job or scope")
        # Atomic replacement also flushes the directory; a crash cannot lose an
        # unsynced unlink and resurrect an apparently cleared reservation.
        atomic_write_lines(self.reservation_path, ["null"])


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
def data_access(paths: Iterable[str | Path], *, updating: bool = False,
                job_id: str | None = None) -> Generator[DataLease]:
    """Refuse overlapping live writers/maintenance without waiting or killing them.

    Writers claim roots in one namespace and ancestors in the other; maintenance
    does the reverse, exclusively. Neither a parent nor a child store can bypass
    the other's lease. Disjoint runtime data stays available; maintenance jobs in
    this account serialize deliberately rather than require a live lock registry.
    Leases are nonblocking, ordered and released together after partial failure.
    An exclusive job may reserve its roots across process handoff. Only that same
    job and exact scope can resume maintenance; a crash does not lift the fence.
    """
    roots: set[Path] = set()
    for raw in paths:
        path = Path(raw)
        if not path.is_absolute():
            raise ValueError("Data stores must use absolute paths")
        roots.add(path.resolve())
    if job_id is not None and (not updating or not roots or str(UUID(job_id)) != job_id):
        raise ValueError("Data maintenance requires an exclusive canonical job identity")
    canonical = tuple(sorted({str(path).casefold() for path in roots}))
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
            if updating:
                # One reservation per account, including Windows stores on
                # different drives that have no shared filesystem ancestor.
                stack.enter_context(file_access(directory / "maintenance.lock", exclusive=True))
            for namespace, key in sorted(leases):
                name = hashlib.sha256(os.fsencode(key)).hexdigest()
                stack.enter_context(file_access(directory / f"{namespace}-{name}.lock", exclusive=updating))
        except BlockingIOError as exc:
            raise InstallationBusyError(
                "Nanobot data is in use by another runtime or update. Stop its writers "
                "before retrying; no data lease was granted."
            ) from exc
        path = directory / "maintenance.json"
        pending = _reservation(path)
        if pending is not None:
            owner, reserved = pending
            if updating:
                if (job_id, canonical) != (owner, reserved):
                    raise InstallationBusyError("Data maintenance belongs to an unfinished update; resume that job first")
            else:
                requested = set(map(Path, canonical))
                ancestors = {parent for root in requested for parent in (root, *root.parents)}
                if any(root in ancestors or any(parent in requested for parent in root.parents)
                       for root in map(Path, reserved)):
                    raise InstallationBusyError("Nanobot data is reserved by an unfinished update; check its saved job before restarting")
        yield DataLease(canonical, path, job_id)
