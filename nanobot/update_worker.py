"""Standalone, offline installer worker for an already prepared update.

This file and its stdlib-only helpers are copied to an immutable zipapp outside
site-packages before mutation. The coordinator uses the base Python. Its installer child
owns the runtime lease itself, so killing the coordinator cannot release that lease
while the installer is still changing files. Only lease-protected child entrypoints import
the installed nanobot runtime for pre-install shutdown or post-install restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from email.parser import BytesParser
from enum import Enum
from pathlib import Path
from typing import cast
from uuid import UUID
from zipfile import ZipFile

from nanobot.data_access import data_access
from nanobot.installation_lock import (
    InstallationBusyError,
    file_access,
    installation_access,
    installation_state_dir,
    pending_update_path,
)
from nanobot.update_backup import BackupSource, ensure_backup, verify_backup
from nanobot.update_uv import UVPreparation, uv_environment
from nanobot.utils.atomic_file import atomic_write_lines

_REQUEST_SCHEMA_VERSION = 4
_MAX_REQUEST_BYTES = 16 * 1024 * 1024

_PROBE = """
import hashlib, json, re, sys
from pathlib import Path
from importlib.metadata import distributions, distribution, PackageNotFoundError
root = Path(sys.prefix).resolve()
cfg = root / 'pyvenv.cfg'
if sys.prefix == sys.base_prefix or not cfg.is_file():
    raise SystemExit('A dedicated virtual environment is required')
owners = [name for name in ('uv-receipt.toml', 'pipx_metadata.json') if (root / name).exists()]
if owners != (['uv-receipt.toml'] if sys.argv[1] == 'uv' else []):
    raise SystemExit('This environment belongs to another package manager')
if 'include-system-site-packages = true' in cfg.read_text().lower():
    raise SystemExit('Shared system packages are not supported by this worker')
items, versions = {}, {}
for d in distributions():
    name = re.sub(r'[-_.]+', '-', d.metadata['Name']).lower()
    payload = '\\n'.join(d.read_text(f) or '' for f in ('METADATA', 'RECORD', 'direct_url.json'))
    items[name] = hashlib.sha256(payload.encode()).hexdigest()
    versions[name] = d.version
try:
    app = distribution('nanobot-ai')
    app_version = app.version
    app_records = {name: hashlib.sha256((app.read_text(name) or '').encode()).hexdigest()
                   for name in ('RECORD', 'direct_url.json')}
except PackageNotFoundError:
    app_version, app_records = '', {}
print(json.dumps(dict(environment=str(root), python=str(Path(sys.executable).absolute()),
    base_python=str(Path(sys._base_executable).resolve()),
    receipt_sha=hashlib.sha256(cfg.read_bytes()).hexdigest(), distributions=items, versions=versions,
    app_version=app_version, app_records=app_records,
    manager_receipt_sha=hashlib.sha256((root / owners[0]).read_bytes()).hexdigest() if owners else None)))
"""


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    data = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in data):
        raise ValueError("Expected string record keys")
    return cast(dict[str, object], data)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("Expected a bounded nonempty string")
    return value


def _sha(value: object) -> str:
    text = _text(value)
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ValueError("Invalid checksum")
    return text


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Expected a nonnegative integer")
    return value


def read_record(path: Path, *, max_bytes: int = 1024 * 1024) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError("Invalid update record")
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("Update record exceeds its size limit")
    return _object(json.loads(data))


def write_record(path: Path, record: object, *, max_bytes: int = 1024 * 1024) -> None:
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) + 1 > max_bytes:
        raise ValueError("Update record exceeds its size limit")
    atomic_write_lines(path, [encoded])
    path.chmod(0o600)


@dataclass(frozen=True)
class EnvironmentSnapshot:
    environment: str
    python: str
    base_python: str
    receipt_sha: str
    distributions: dict[str, str]
    versions: dict[str, str]
    app_version: str
    app_records: dict[str, str]
    manager_receipt_sha: str | None = None

    @classmethod
    def parse(cls, value: object) -> EnvironmentSnapshot:
        data = _object(value)
        version = data["app_version"]
        if not isinstance(version, str):
            raise ValueError("Invalid installed version")
        return cls(_text(data["environment"]), _text(data["python"]), _text(data["base_python"]),
                   _sha(data["receipt_sha"]), {name: _sha(digest) for name, digest in
                                             _object(data["distributions"]).items()},
                   {name: _text(value) for name, value in _object(data["versions"]).items()}, version,
                   {name: _sha(digest) for name, digest in _object(data["app_records"]).items()},
                   _sha(data["manager_receipt_sha"]) if data.get("manager_receipt_sha") is not None else None)


def inspect_environment(python: str, *, uv: bool = False) -> EnvironmentSnapshot:
    result = subprocess.run([python, "-I", "-c", _PROBE, "uv" if uv else "pip"],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise ValueError("Could not verify the selected package environment; use its installation owner")
    return EnvironmentSnapshot.parse(json.loads(result.stdout))


@dataclass(frozen=True)
class StagedWheel:
    filename: str
    name: str
    version: str
    size: int
    sha256: str

    @classmethod
    def parse(cls, value: object) -> StagedWheel:
        data = _object(value)
        filename, name = _text(data["filename"]), _text(data["name"])
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.whl", filename) or not re.fullmatch(r"[a-z0-9-]+", name):
            raise ValueError("Invalid staged wheel name")
        if name == "pip":
            raise ValueError("An application update cannot replace its installer")
        return cls(filename, name, _text(data["version"]), _integer(data["size"]), _sha(data["sha256"]))

    def verify(self, directory: Path) -> Path:
        path = directory / self.filename
        if path.is_symlink() or path.stat().st_size != self.size:
            raise ValueError("Staged wheel size or file type changed")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != self.sha256:
                raise ValueError("Staged wheel checksum changed")
        with ZipFile(path) as wheel:
            names = wheel.namelist()
            if len(names) != len(set(names)) or any(re.search(r"(^[A-Za-z]:|^/|\\|(?:^|/)\.\.(?:/|$))", name) for name in names):
                raise ValueError("Invalid wheel paths")
            metadata = [item for item in wheel.infolist() if item.filename.endswith(".dist-info/METADATA")]
            if len(metadata) != 1 or metadata[0].file_size > 256 * 1024:
                raise ValueError("Invalid wheel metadata")
            parsed = BytesParser().parsebytes(wheel.read(metadata[0]))
            name = re.sub(r"[-_.]+", "-", str(parsed["Name"])).lower()
            if (name, parsed["Version"]) != (self.name, self.version):
                raise ValueError("Staged wheel identity changed")
        return path


@dataclass(frozen=True)
class WorkerSpec:
    plan_id: str
    plan_sha256: str
    worker_sha256: str
    baseline: EnvironmentSnapshot
    version: str
    wheels: tuple[StagedWheel, ...]
    backup_sources: tuple[BackupSource, ...]
    restart_profiles: tuple[str, ...] = ()
    uv: UVPreparation | None = None

    @classmethod
    def read(cls, directory: Path) -> WorkerSpec:
        data = read_record(directory / "request.json", max_bytes=_MAX_REQUEST_BYTES)
        if data.get("schema_version") != _REQUEST_SCHEMA_VERSION:
            raise ValueError("Unsupported update worker request")
        plan_id = _text(data["plan_id"])
        if str(UUID(plan_id)) != plan_id or directory.name != plan_id:
            raise ValueError("Update job identity mismatch")
        raw_wheels = data["wheels"]
        if not isinstance(raw_wheels, list):
            raise ValueError("Invalid update wheel set")
        wheel_values = cast(list[object], raw_wheels)
        if not 1 <= len(wheel_values) <= 500:
            raise ValueError("Invalid update wheel set")
        wheels = tuple(StagedWheel.parse(value) for value in wheel_values)
        raw_sources = data.get("backup_sources")
        if not isinstance(raw_sources, list):
            raise ValueError("Invalid backup scope")
        source_values = cast(list[object], raw_sources)
        if not 1 <= len(source_values) <= 40_000:
            raise ValueError("Invalid backup scope")
        sources = tuple(BackupSource.parse(value) for value in source_values)
        participants = data.get("restart_profiles")
        if not isinstance(participants, list):
            raise ValueError("Invalid restart participants")
        restart_profiles = tuple(_sha(value) for value in cast(list[object], participants))
        if len(restart_profiles) > 1024 or len(set(restart_profiles)) != len(restart_profiles):
            raise ValueError("Invalid restart participants")
        spec = cls(plan_id, _sha(data["plan_sha256"]), _sha(data["worker_sha256"]), EnvironmentSnapshot.parse(data["baseline"]),
                   _text(data["version"]), wheels, sources, restart_profiles,
                   UVPreparation.parse(data["uv"]) if data.get("uv") is not None else None)
        if (spec.uv is None) != (spec.baseline.manager_receipt_sha is None):
            raise ValueError("Installer preparation does not match the environment owner")
        if spec.uv is not None and (spec.uv.receipt_sha256 != spec.baseline.manager_receipt_sha
                                    or spec.uv.versions.get("nanobot-ai") != spec.version):
            raise ValueError("The uv preparation does not match the approved installation or target")
        if directory.resolve() != (installation_state_dir(spec.baseline.environment) / "jobs" / plan_id).resolve():
            raise ValueError("Update job is outside its installation state directory")
        if len({wheel.name for wheel in wheels}) != len(wheels):
            raise ValueError("Duplicate staged distributions")
        if [(wheel.name, wheel.version) for wheel in wheels if wheel.name == "nanobot-ai"] != [("nanobot-ai", spec.version)]:
            raise ValueError("Missing exact nanobot target")
        if hashlib.sha256((directory / "plan.json").read_bytes()).hexdigest() != spec.plan_sha256:
            raise ValueError("The approved plan changed")
        if hashlib.sha256((directory / "worker.pyz").read_bytes()).hexdigest() != spec.worker_sha256:
            raise ValueError("The staged update worker changed")
        return spec

    def save(self, directory: Path) -> None:
        # Expanded profile stores may exceed the small journal/approval limit.
        # Keep the writer and standalone reader on the same bounded contract.
        write_record(directory / "request.json", {"schema_version": _REQUEST_SCHEMA_VERSION, **asdict(self)},
                     max_bytes=_MAX_REQUEST_BYTES)

    def verify_inputs(self, directory: Path) -> None:
        for wheel in self.wheels:
            wheel.verify(directory / "wheels")
        if self.uv is not None:
            self.uv.verify(self.baseline.environment, directory)


class Phase(str, Enum):
    READY = "ready"
    CHECKING = "checking"
    STOPPING = "stopping"
    HANDOFF_FAILED = "handoff_failed"
    BACKING_UP = "backing_up"
    INSTALLING = "installing"
    VERIFYING = "verifying"
    INSTALLED = "installed"
    RESTARTING = "restarting"
    ACTIVATING = "activating"
    FINALIZING = "finalizing"
    RESTART_FAILED = "restart_failed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class JobStatus:
    plan_id: str
    phase: Phase
    attempt: int
    mutation_started: bool
    message: str
    updated_at: int

    @classmethod
    def read(cls, directory: Path) -> JobStatus:
        value = read_record(directory / "status.json")
        mutated = value["mutation_started"]
        if type(mutated) is not bool or value["plan_id"] != directory.name:
            raise ValueError("Invalid update status")
        return cls(directory.name, Phase(value["phase"]), _integer(value["attempt"]), mutated,
                   _text(value["message"]), _integer(value["updated_at"]))

    def save(self, directory: Path) -> None:
        write_record(directory / "status.json", asdict(self))


def _check_approval(directory: Path, spec: WorkerSpec, *, handoff: bool = False) -> dict[str, object]:
    approval = read_record(directory / ("handoff-approval.json" if handoff else "approval.json"))
    now = int(time.time())
    created, expires = _integer(approval["created_at"]), _integer(approval["expires_at"])
    if not created <= now < expires <= created + 900:
        raise ValueError("Update approval expired; review this exact job again before retrying")
    digest = hashlib.sha256((directory / "request.json").read_bytes()).hexdigest()
    if approval.get("request_sha256") != digest or approval.get("plan_id") != spec.plan_id:
        raise ValueError("Update approval does not match this job")
    return approval


def _prepare_handoff_child(directory: Path) -> None:
    spec = WorkerSpec.read(directory)
    if Path(sys.prefix).resolve() != Path(spec.baseline.environment):
        raise ValueError("Shutdown handoff is using the wrong environment")
    # The child imports the old application, but keeps a shared lease until it
    # actually exits. Parent loss cannot allow another installer to replace its
    # imports. The base-Python parent waits for this exit before changing files.
    with (file_access(installation_state_dir(spec.baseline.environment) / "handoff-child.lock", exclusive=True),
          installation_access()):
        _check_approval(directory, spec, handoff=True)
        from nanobot.update_handoff import prepare_handoff

        prepare_handoff(directory)


def _handoff(directory: Path, spec: WorkerSpec) -> None:
    status = JobStatus.read(directory)
    _check_approval(directory, spec, handoff=True)
    JobStatus(spec.plan_id, Phase.CHECKING, status.attempt, False,
              "Rechecking the exact release and installation before shutdown", int(time.time())).save(directory)
    subprocess.run([spec.baseline.python, "-I", str(directory / "worker.pyz"),
                    "--prepare-handoff", str(directory)], check=True, timeout=300,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    approval = _check_approval(directory, spec, handoff=True)
    with (installation_access(updating=True, environment=spec.baseline.environment),
          data_access((source.path for source in spec.backup_sources), updating=True, job_id=spec.plan_id)):
        check_environment(spec, repairing=False)
        spec.verify_inputs(directory)
        # Stopping a process did not grant installation authority. Only successful
        # preflight, child exit and exclusive installation/data leases get here.
        write_record(directory / "approval.json", approval)


def check_environment(spec: WorkerSpec, *, repairing: bool) -> None:
    current = inspect_environment(spec.baseline.python, uv=spec.uv is not None)
    if (current.environment, current.python, current.base_python, current.receipt_sha, current.manager_receipt_sha) != (
        spec.baseline.environment, spec.baseline.python, spec.baseline.base_python, spec.baseline.receipt_sha,
        spec.baseline.manager_receipt_sha,
    ):
        raise ValueError("Installation ownership or interpreter changed")
    changed: set[str] = {wheel.name for wheel in spec.wheels} if repairing else set()
    targets = {wheel.name: wheel.version for wheel in spec.wheels}
    if spec.uv is not None:
        targets = spec.uv.versions
        if repairing:
            changed = set(spec.baseline.versions) | set(targets)
    before = {name: digest for name, digest in spec.baseline.distributions.items() if name not in changed}
    after = {name: digest for name, digest in current.distributions.items() if name not in changed}
    if before != after:
        raise ValueError("The package environment changed outside this update; review it before continuing")
    if repairing:
        for name in changed:
            if current.versions.get(name) not in {None, spec.baseline.versions.get(name), targets.get(name)}:
                raise ValueError("Another update changed a target package; repair cannot overwrite its version")


def _pending_owner(spec: WorkerSpec) -> None:
    pending = pending_update_path(spec.baseline.environment)
    if pending.exists() and read_record(pending).get("job_id") != spec.plan_id:
        raise ValueError("Another incomplete update owns this installation")


def _installer_child(directory: Path) -> None:
    spec = WorkerSpec.read(directory)
    if Path(sys.prefix).resolve() != Path(spec.baseline.environment):
        raise ValueError("Installer child is using the wrong environment")
    with (installation_access(updating=True) as installation,
          data_access((source.path for source in spec.backup_sources), updating=True, job_id=spec.plan_id) as data):
        status = JobStatus.read(directory)
        if status.phase in {Phase.INSTALLED, Phase.CANCELLED, Phase.RESTARTING,
                            Phase.ACTIVATING, Phase.FINALIZING, Phase.RESTART_FAILED, Phase.SUCCEEDED}:
            raise ValueError("This update job is already finished")
        _check_approval(directory, spec)
        _pending_owner(spec)
        check_environment(spec, repairing=status.mutation_started)
        wheels = [str(wheel.verify(directory / "wheels")) for wheel in spec.wheels]
        uv_cache = None
        if spec.uv is not None:
            if os.name != "posix":
                raise ValueError("Native uv lease handoff is not available on this platform")
            spec.uv.verify(spec.baseline.environment, directory)
            uv_cache = spec.uv.working_cache(directory)
        verify_backup(directory, spec.backup_sources, hashlib.sha256((directory / "request.json").read_bytes()).hexdigest(),
                      unchanged=not status.mutation_started)
        pending = pending_update_path()
        pending.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data.reserve()
        write_record(pending, {"schema_version": 2, "job_id": spec.plan_id, "version": spec.version})
        JobStatus(spec.plan_id, Phase.INSTALLING, status.attempt + 1, True,
                  "Installing the verified package files", int(time.time())).save(directory)
        if spec.uv is not None and uv_cache is not None:
            # Exact-wheel override is one operation, not a rewrite of the saved
            # requirement. Even --no-build would persist in the user's receipt.
            plan = read_record(directory / "plan.json", max_bytes=_MAX_REQUEST_BYTES)
            extras = plan.get("extras")
            if not isinstance(extras, list):
                raise ValueError("Invalid approved uv extras")
            extra_values = cast(list[object], extras)
            if not all(isinstance(extra, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", extra)
                       for extra in extra_values):
                raise ValueError("Invalid approved uv extras")
            suffix = "[" + ",".join(cast(list[str], extra_values)) + "]" if extras else ""
            app = next(wheel for wheel in spec.wheels if wheel.name == "nanobot-ai")
            target = "nanobot-ai" + suffix + " @ " + (directory / "wheels" / app.filename).as_uri()
            env = uv_environment(tools=Path(spec.baseline.environment).parent,
                                 binaries=Path(spec.uv.binaries), cache=uv_cache)
            arguments = [spec.uv.executable, "tool", "upgrade", "--no-config", "--offline", "--reinstall", target]
            with installation.inherit_for_exec(), data.inherit_for_exec():
                os.execve(spec.uv.executable, arguments, env)
        # The installer itself must own the leases, not a wrapper which can die
        # while a child keeps writing. POSIX exec retains the worker's PID and
        # leases. Windows stays in this lease-owning process instead of spawning
        # a child: inherited Windows handles do not transfer byte-range locks.
        # Only verified local wheels are accepted, so no build child can outlive
        # the installer. The coordinator performs verification separately.
        arguments = ["--disable-pip-version-check", "--no-input", "install",
                     "--no-index", "--no-deps", "--only-binary=:all:", "--no-compile",
                     "--force-reinstall", *wheels]
        if os.name == "posix":
            with installation.inherit_for_exec(), data.inherit_for_exec():
                os.execve(sys.executable, [sys.executable, "-I", "-m", "pip", *arguments], os.environ)
        # A dedicated CLI, ending in pip's SystemExit; no application or second
        # pip call runs afterwards. This is not pip's internal Python API.
        sys.argv = ["pip", *arguments]
        runpy.run_module("pip", run_name="__main__")


def _verify(spec: WorkerSpec, directory: Path) -> None:
    probe = (
        "import json,nanobot; from pathlib import Path; "
        "print(json.dumps([nanobot.__version__,str(Path(nanobot.__file__).resolve())]))"
    )
    result = subprocess.run([spec.baseline.python, "-I", "-c", probe], capture_output=True,
                            text=True, timeout=30)
    if result.returncode:
        raise ValueError("The installed nanobot package does not import")
    version, package = json.loads(result.stdout)
    if version != spec.version or not Path(package).is_relative_to(spec.baseline.environment):
        raise ValueError("Installed package version or location differs from the approved target")
    app = next(wheel for wheel in spec.wheels if wheel.name == "nanobot-ai")
    with ZipFile(directory / "wheels" / app.filename) as wheel:
        for item in wheel.infolist():
            if item.is_dir() or not item.filename.startswith("nanobot/"):
                continue
            installed = Path(package).parent.parent / item.filename
            if not installed.is_file() or installed.is_symlink():
                raise ValueError("An installed application file is missing")
            with installed.open("rb") as actual, wheel.open(item) as expected:
                digest = hashlib.sha256()
                while chunk := expected.read(1024 * 1024):
                    digest.update(chunk)
                if hashlib.file_digest(actual, "sha256").digest() != digest.digest():
                    raise ValueError("Installed application files differ from the verified wheel")
    if spec.uv is not None:
        spec.uv.verify(spec.baseline.environment, directory)
        current = inspect_environment(spec.baseline.python, uv=True)
        if (current.environment, current.python, current.base_python, current.receipt_sha) != (
            spec.baseline.environment, spec.baseline.python, spec.baseline.base_python, spec.baseline.receipt_sha,
        ):
            raise ValueError("uv changed the prepared interpreter or environment ownership")
        if current.versions != spec.uv.versions:
            raise ValueError("The installed uv dependencies differ from the prepared transaction")
        command = [spec.uv.executable, "pip", "check", "--no-config", "--no-cache", "--offline", "--python", spec.baseline.python]
        env = uv_environment(tools=Path(spec.baseline.environment).parent,
                             binaries=Path(spec.uv.binaries), cache=directory / ".uv-check-cache")
    else:
        command = [spec.baseline.python, "-I", "-m", "pip", "check"]
        env = None
    result = subprocess.run(command,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, env=env)
    if result.returncode:
        raise ValueError("Installed dependency verification failed")


def run_job(directory: Path, *, handoff: bool = False) -> JobStatus:
    """Execute one approved offline job. 'installed' does not mean restarted."""
    spec = WorkerSpec.read(directory)
    root = installation_state_dir(spec.baseline.environment)
    with file_access(root / "worker.lock", exclusive=True):
        if handoff:
            # An orphan preflight child may still own the old runtime. Refuse a
            # competing handoff without rewriting its durable progress.
            with file_access(root / "handoff-child.lock", exclusive=True):
                pass
        status = JobStatus.read(directory)
        if status.phase == Phase.CANCELLED:
            raise ValueError("This update job was cancelled")
        if handoff and (status.mutation_started or status.phase not in {
            Phase.READY, Phase.CHECKING, Phase.STOPPING, Phase.HANDOFF_FAILED,
        }):
            raise ValueError("Shutdown handoff requires an unmodified prepared job")
        try:
            if handoff:
                spec.verify_inputs(directory)
                _handoff(directory, spec)
            if status.phase in {Phase.RESTARTING, Phase.ACTIVATING, Phase.FINALIZING, Phase.RESTART_FAILED, Phase.SUCCEEDED}:
                return _restart_runtime(directory, spec)
            with (installation_access(updating=True, environment=spec.baseline.environment),
                  data_access((source.path for source in spec.backup_sources), updating=True, job_id=spec.plan_id) as data):
                _pending_owner(spec)
                if status.phase == Phase.INSTALLED:
                    # Recover a crash after persisting the result but before removing
                    # the startup fence. No reinstall or new consent is necessary.
                    if pending_update_path(spec.baseline.environment).exists():
                        _verify(spec, directory)
                        pending_update_path(spec.baseline.environment).unlink()
                    data.release_reservation()
                    return status
                _check_approval(directory, spec)
                check_environment(spec, repairing=status.mutation_started)
                spec.verify_inputs(directory)
                JobStatus(spec.plan_id, Phase.BACKING_UP, status.attempt, status.mutation_started,
                          "Preserving the original scoped data backup", int(time.time())).save(directory)
                ensure_backup(directory, spec.backup_sources,
                              hashlib.sha256((directory / "request.json").read_bytes()).hexdigest(),
                              mutated=status.mutation_started)
                # Block shared-data writers too, including a different Python
                # installation, across handoff and interrupted-job recovery.
                data.reserve()
                pending = pending_update_path(spec.baseline.environment)
                pending.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                write_record(pending, {"schema_version": 2, "job_id": spec.plan_id, "version": spec.version})
            env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PIP_")}
            env["PIP_CONFIG_FILE"] = os.devnull
            subprocess.run([spec.baseline.python, "-I", str(directory / "worker.pyz"),
                            "--install", str(directory)], check=True, timeout=900,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            with (installation_access(updating=True, environment=spec.baseline.environment),
                  data_access((source.path for source in spec.backup_sources), updating=True, job_id=spec.plan_id) as data):
                status = JobStatus.read(directory)
                JobStatus(spec.plan_id, Phase.VERIFYING, status.attempt, True,
                          "Checking the installed package and dependencies", int(time.time())).save(directory)
                _verify(spec, directory)
                status = JobStatus(spec.plan_id, Phase.RESTARTING if spec.restart_profiles else Phase.INSTALLED,
                                   status.attempt, True,
                                   "Files installed and checked; running gateway not yet verified", int(time.time()))
                status.save(directory)
                if not spec.restart_profiles:
                    pending_update_path(spec.baseline.environment).unlink()
                    data.release_reservation()
                    return status
            return _restart_runtime(directory, spec)
        except InstallationBusyError:
            # An orphan pip child may still be alive. Do not turn its ongoing
            # installation into a terminal failure or overwrite its journal.
            current = JobStatus.read(directory)
            if handoff and current.phase in {Phase.CHECKING, Phase.STOPPING} and not current.mutation_started:
                JobStatus(spec.plan_id, Phase.HANDOFF_FAILED, current.attempt, False,
                          "Update handoff could not acquire exclusive access; no package files were changed",
                          int(time.time())).save(directory)
            raise
        except Exception as exc:
            status = JobStatus.read(directory)
            if status.phase in {Phase.FINALIZING, Phase.SUCCEEDED}:
                # Runtime success was durably verified. Retry only final cleanup,
                # never demote it to a restart or package-repair transaction.
                raise
            # Do not persist arbitrary package-manager output, URLs or credentials.
            message = str(exc) if isinstance(exc, (ValueError, InstallationBusyError)) else f"Update failed ({type(exc).__name__})"
            handoff_failed = status.phase in {Phase.CHECKING, Phase.STOPPING, Phase.HANDOFF_FAILED}
            if handoff_failed:
                # The old runtime may already have exited. Never imply that a
                # pre-install failure undoes a durable shutdown commitment.
                message = (status.message if status.phase == Phase.HANDOFF_FAILED else
                           "Update handoff did not finish; review the saved shutdown state before retrying")
            phase = Phase.HANDOFF_FAILED if handoff_failed else (
                Phase.RESTART_FAILED if status.phase in {
                    Phase.RESTARTING, Phase.ACTIVATING, Phase.RESTART_FAILED,
                } else Phase.FAILED
            )
            failed = JobStatus(spec.plan_id, phase, status.attempt, status.mutation_started,
                               message[:1000], int(time.time()))
            failed.save(directory)
            if not status.mutation_started and not handoff_failed:
                with (installation_access(updating=True, environment=spec.baseline.environment),
                      data_access((source.path for source in spec.backup_sources), updating=True, job_id=spec.plan_id) as data):
                    _pending_owner(spec)
                    pending_update_path(spec.baseline.environment).unlink(missing_ok=True)
                    data.release_reservation()
            raise


def _restart_runtime(directory: Path, spec: WorkerSpec) -> JobStatus:
    # Use the target Python, but enter through frozen stdlib code so its shared
    # installation lease is held before importing the mutable target runtime.
    subprocess.run([spec.baseline.python, "-I", str(directory / "worker.pyz"),
                    "--restart", str(directory)], check=True, timeout=300,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = JobStatus.read(directory)
    if result.phase != Phase.SUCCEEDED:
        raise ValueError("Runtime handoff did not verify the updated gateways")
    return result


def _runtime_child(directory: Path, participant: str | None = None) -> None:
    spec = WorkerSpec.read(directory)
    if Path(sys.prefix).resolve() != Path(spec.baseline.environment) or not spec.restart_profiles:
        raise ValueError("Runtime handoff is using the wrong environment or participants")
    with installation_access(allow_incomplete=True):
        _pending_owner(spec)
        # Deliberately import the installed application, not a frozen stub.
        from nanobot.update_restart import run_gateway, run_restarts

        if participant is None:
            run_restarts(directory)
        elif participant in spec.restart_profiles:
            run_gateway(directory, participant)
        else:
            raise ValueError("This gateway is not a reviewed restart participant")


def cancel_job(directory: Path) -> JobStatus:
    """Cancel only a stopped, unmodified job; never terminate an installer."""
    spec = WorkerSpec.read(directory)
    with file_access(installation_state_dir(spec.baseline.environment) / "worker.lock", exclusive=True):
        with (installation_access(updating=True, environment=spec.baseline.environment),
              data_access((source.path for source in spec.backup_sources), updating=True, job_id=spec.plan_id) as data):
            status = JobStatus.read(directory)
            if status.mutation_started:
                raise ValueError("Installation has begun; this job needs verification or repair, not cancellation")
            _pending_owner(spec)
            pending_update_path(spec.baseline.environment).unlink(missing_ok=True)
            data.release_reservation()
            status = JobStatus(spec.plan_id, Phase.CANCELLED, status.attempt, False,
                               "Cancelled; installed files were not changed", int(time.time()))
            status.save(directory)
            (directory / "approval.json").unlink(missing_ok=True)
            return status


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--prepare-handoff":
        _prepare_handoff_child(Path(sys.argv[2]).resolve())
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--restart":
        _runtime_child(Path(sys.argv[2]).resolve())
        return
    if len(sys.argv) == 4 and sys.argv[1] == "--gateway":
        _runtime_child(Path(sys.argv[2]).resolve(), sys.argv[3])
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--install":
        _installer_child(Path(sys.argv[2]).resolve())
        return
    if len(sys.argv) == 3 and sys.argv[1] in {"--status", "--cancel"}:
        directory = Path(sys.argv[2]).resolve()
        WorkerSpec.read(directory)
        status = cancel_job(directory) if sys.argv[1] == "--cancel" else JobStatus.read(directory)
        print(json.dumps(asdict(status)))
        return
    handoff = len(sys.argv) == 3 and sys.argv[1] == "--handoff"
    if len(sys.argv) != 2 and not handoff:
        raise SystemExit("Use this worker with its exact saved job directory")
    directory = Path(sys.argv[2] if handoff else sys.argv[1]).resolve()
    try:
        result = run_job(directory, handoff=handoff)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Update did not finish: {type(exc).__name__}. Consult the saved job status.", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(asdict(result)))


if __name__ == "__main__":
    main()
