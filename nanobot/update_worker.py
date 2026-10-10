"""Standalone, offline pip worker for an already prepared plain-venv update.

This file and its two stdlib-only helpers are copied to an immutable zipapp outside
site-packages before mutation. The coordinator uses the base Python. Its pip child
owns the runtime lease itself, so killing the coordinator cannot release that lease
while pip is still changing files. Nothing in this module imports the nanobot runtime.
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

from nanobot.installation_lock import (
    InstallationBusyError,
    file_access,
    installation_access,
    installation_state_dir,
    pending_update_path,
)
from nanobot.utils.atomic_file import atomic_write_lines

_PROBE = """
import hashlib, json, re, sys
from pathlib import Path
from importlib.metadata import distributions, distribution, PackageNotFoundError
root = Path(sys.prefix).resolve()
cfg = root / 'pyvenv.cfg'
if sys.prefix == sys.base_prefix or not cfg.is_file():
    raise SystemExit('A dedicated virtual environment is required')
if any((root / name).exists() for name in ('uv-receipt.toml', 'pipx_metadata.json')):
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
    app_version=app_version, app_records=app_records)))
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


def read_record(path: Path) -> dict[str, object]:
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Invalid update record")
    return _object(json.loads(path.read_bytes()))


def write_record(path: Path, record: object) -> None:
    atomic_write_lines(path, [json.dumps(record, sort_keys=True, separators=(",", ":"))])
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
                   {name: _sha(digest) for name, digest in _object(data["app_records"]).items()})


def inspect_environment(python: str) -> EnvironmentSnapshot:
    result = subprocess.run([python, "-I", "-c", _PROBE], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise ValueError("Could not verify the dedicated pip environment; use its installation owner")
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

    @classmethod
    def read(cls, directory: Path) -> WorkerSpec:
        data = read_record(directory / "request.json")
        if data.get("schema_version") != 1:
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
        spec = cls(plan_id, _sha(data["plan_sha256"]), _sha(data["worker_sha256"]), EnvironmentSnapshot.parse(data["baseline"]),
                   _text(data["version"]), wheels)
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


class Phase(str, Enum):
    READY = "ready"
    INSTALLING = "installing"
    VERIFYING = "verifying"
    INSTALLED = "installed"
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


def _check_approval(directory: Path, spec: WorkerSpec) -> None:
    approval = read_record(directory / "approval.json")
    now = int(time.time())
    created, expires = _integer(approval["created_at"]), _integer(approval["expires_at"])
    if not created <= now < expires <= created + 900:
        raise ValueError("Update approval expired; review this exact job again before retrying")
    digest = hashlib.sha256((directory / "request.json").read_bytes()).hexdigest()
    if approval.get("request_sha256") != digest or approval.get("plan_id") != spec.plan_id:
        raise ValueError("Update approval does not match this job")


def check_environment(spec: WorkerSpec, *, repairing: bool) -> None:
    current = inspect_environment(spec.baseline.python)
    if (current.environment, current.python, current.base_python, current.receipt_sha) != (
        spec.baseline.environment, spec.baseline.python, spec.baseline.base_python, spec.baseline.receipt_sha,
    ):
        raise ValueError("Installation ownership or interpreter changed")
    changed: set[str] = {wheel.name for wheel in spec.wheels} if repairing else set()
    before = {name: digest for name, digest in spec.baseline.distributions.items() if name not in changed}
    after = {name: digest for name, digest in current.distributions.items() if name not in changed}
    if before != after:
        raise ValueError("The package environment changed outside this update; review it before continuing")
    if repairing:
        for wheel in spec.wheels:
            if current.versions.get(wheel.name) not in {None, spec.baseline.versions.get(wheel.name), wheel.version}:
                raise ValueError("Another update changed a target package; repair cannot overwrite its version")


def _pending_owner(spec: WorkerSpec) -> None:
    pending = pending_update_path(spec.baseline.environment)
    if pending.exists() and read_record(pending).get("job_id") != spec.plan_id:
        raise ValueError("Another incomplete update owns this installation")


def _pip_child(directory: Path) -> None:
    spec = WorkerSpec.read(directory)
    if Path(sys.prefix).resolve() != Path(spec.baseline.environment):
        raise ValueError("Installer child is using the wrong environment")
    with installation_access(updating=True):
        status = JobStatus.read(directory)
        if status.phase in {Phase.INSTALLED, Phase.CANCELLED}:
            raise ValueError("This update job is already finished")
        _check_approval(directory, spec)
        _pending_owner(spec)
        check_environment(spec, repairing=status.mutation_started)
        wheels = [str(wheel.verify(directory / "wheels")) for wheel in spec.wheels]
        pending = pending_update_path()
        pending.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_record(pending, {"schema_version": 2, "job_id": spec.plan_id, "version": spec.version})
        JobStatus(spec.plan_id, Phase.INSTALLING, status.attempt + 1, True,
                  "Installing the verified package files", int(time.time())).save(directory)
        # This is a dedicated CLI process, not pip's internal Python API. It ends
        # with pip's SystemExit; no application or second pip call runs afterwards.
        # Only verified local wheels are accepted, so no build subprocess can outlive
        # this process's lease. The coordinator performs verification separately.
        sys.argv = ["pip", "--disable-pip-version-check", "--no-input", "install",
                    "--no-index", "--no-deps", "--only-binary=:all:", "--no-compile",
                    "--force-reinstall", *wheels]
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
    result = subprocess.run([spec.baseline.python, "-I", "-m", "pip", "check"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    if result.returncode:
        raise ValueError("Installed dependency verification failed")


def run_job(directory: Path) -> JobStatus:
    """Execute one approved offline job. 'installed' does not mean restarted."""
    spec = WorkerSpec.read(directory)
    root = installation_state_dir(spec.baseline.environment)
    with file_access(root / "worker.lock", exclusive=True):
        status = JobStatus.read(directory)
        if status.phase == Phase.CANCELLED:
            raise ValueError("This update job was cancelled")
        try:
            with installation_access(updating=True, environment=spec.baseline.environment):
                _pending_owner(spec)
                if status.phase == Phase.INSTALLED:
                    # Recover a crash after persisting the result but before removing
                    # the startup fence. No reinstall or new consent is necessary.
                    if pending_update_path(spec.baseline.environment).exists():
                        _verify(spec, directory)
                        pending_update_path(spec.baseline.environment).unlink()
                    return status
                _check_approval(directory, spec)
                check_environment(spec, repairing=status.mutation_started)
                for wheel in spec.wheels:
                    wheel.verify(directory / "wheels")
                # Block new runtimes across the coordinator -> pip child gap.
                pending = pending_update_path(spec.baseline.environment)
                pending.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                write_record(pending, {"schema_version": 2, "job_id": spec.plan_id, "version": spec.version})
            env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PIP_")}
            env["PIP_CONFIG_FILE"] = os.devnull
            subprocess.run([spec.baseline.python, "-I", str(directory / "worker.pyz"),
                            "--pip", str(directory)], check=True, timeout=900,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            with installation_access(updating=True, environment=spec.baseline.environment):
                status = JobStatus.read(directory)
                JobStatus(spec.plan_id, Phase.VERIFYING, status.attempt, True,
                          "Checking the installed package and dependencies", int(time.time())).save(directory)
                _verify(spec, directory)
                status = JobStatus(spec.plan_id, Phase.INSTALLED, status.attempt, True,
                                   "Files installed and checked; running gateway not yet verified", int(time.time()))
                status.save(directory)
                pending_update_path(spec.baseline.environment).unlink()
                return status
        except InstallationBusyError:
            # An orphan pip child may still be alive. Do not turn its ongoing
            # installation into a terminal failure or overwrite its journal.
            raise
        except Exception as exc:
            status = JobStatus.read(directory)
            # Do not persist arbitrary package-manager output, URLs or credentials.
            message = str(exc) if isinstance(exc, (ValueError, InstallationBusyError)) else f"Update failed ({type(exc).__name__})"
            failed = JobStatus(spec.plan_id, Phase.FAILED, status.attempt, status.mutation_started,
                               message[:1000], int(time.time()))
            failed.save(directory)
            if not status.mutation_started and pending_update_path(spec.baseline.environment).exists():
                with installation_access(updating=True, environment=spec.baseline.environment):
                    _pending_owner(spec)
                    pending_update_path(spec.baseline.environment).unlink(missing_ok=True)
            raise


def cancel_job(directory: Path) -> JobStatus:
    """Cancel only a stopped, unmodified job; never terminate an installer."""
    spec = WorkerSpec.read(directory)
    with file_access(installation_state_dir(spec.baseline.environment) / "worker.lock", exclusive=True):
        with installation_access(updating=True, environment=spec.baseline.environment):
            status = JobStatus.read(directory)
            if status.mutation_started:
                raise ValueError("Installation has begun; this job needs verification or repair, not cancellation")
            _pending_owner(spec)
            pending_update_path(spec.baseline.environment).unlink(missing_ok=True)
            status = JobStatus(spec.plan_id, Phase.CANCELLED, status.attempt, False,
                               "Cancelled; installed files were not changed", int(time.time()))
            status.save(directory)
            (directory / "approval.json").unlink(missing_ok=True)
            return status


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--pip":
        _pip_child(Path(sys.argv[2]).resolve())
        return
    if len(sys.argv) == 3 and sys.argv[1] in {"--status", "--cancel"}:
        directory = Path(sys.argv[2]).resolve()
        WorkerSpec.read(directory)
        status = cancel_job(directory) if sys.argv[1] == "--cancel" else JobStatus.read(directory)
        print(json.dumps(asdict(status)))
        return
    if len(sys.argv) != 2:
        raise SystemExit("Use this worker with its exact saved job directory")
    directory = Path(sys.argv[1]).resolve()
    try:
        result = run_job(directory)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Update did not finish: {type(exc).__name__}. Consult the saved job status.", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(asdict(result)))


if __name__ == "__main__":
    main()
