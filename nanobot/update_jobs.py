"""Prepare exact offline jobs and hand them to the standalone installer worker.

This is the executor's internal Interface, not an authenticated WebUI mutation.
The caller owns explicit shutdown/restart selection. A detached worker revalidates,
stops reviewed idle gateways, backs up data, installs, and restarts selected runtimes.
Until that caller is integrated, CLI/UI cannot start these jobs.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import nullcontext
from email.parser import BytesParser
from pathlib import Path
from typing import ParamSpec, TypeVar
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile, ZipInfo

from packaging.utils import canonicalize_name, parse_wheel_filename
from pydantic import BaseModel, ConfigDict, Field

from nanobot.data_access import data_access
from nanobot.installation_lock import (
    file_access,
    installation_access,
    installation_id,
    installation_state_dir,
)
from nanobot.release_feed import ReleaseFeed
from nanobot.releases import Channel, ReleaseRecord, Sha256
from nanobot.update_backup import BackupSource, profile_sources, unique_sources
from nanobot.update_plan import UpdatePlan
from nanobot.update_profiles import require_stopped_profiles, validate_profiles
from nanobot.update_uv import UVPreparation, prepare_uv
from nanobot.update_worker import (
    JobStatus,
    Phase,
    StagedWheel,
    WorkerSpec,
    check_environment,
    inspect_environment,
    read_record,
    write_record,
)
from nanobot.utils.atomic_file import atomic_write_lines

_Args = ParamSpec("_Args")
_Result = TypeVar("_Result")


class UpdateJobNotFoundError(FileNotFoundError):
    """No saved preparation or execution status exists for this installation."""


class UpdateJobView(ReleaseRecord):
    """Last durable facts only: not proof of a live worker or authority to retry."""

    id: str
    installation_id: str
    host: str
    previous_version: str
    target_version: str
    channel: Channel
    recorded_phase: Phase
    attempt: int
    mutation_started: bool
    updated_at: int


def read_update_job(plan_id: str) -> UpdateJobView:
    """Read the journal scoped to this environment path, without probing Python.

    No interpreter probe, network request or runtime lease is required. A failed
    installation and an active writer must not hide its last recorded state.
    This does not validate continued release eligibility, worker liveness or
    that an environment reinstalled at this path is still the reviewed one.
    """
    UpdatePlan.canonical_id(plan_id)
    directory = installation_state_dir() / "jobs" / plan_id
    if directory.is_symlink():
        raise ValueError("Invalid update job directory")
    try:
        plan = UpdatePlan.model_validate(read_record(directory / "plan.json"))
        status = JobStatus.read(directory)
    except FileNotFoundError as exc:
        raise UpdateJobNotFoundError("No saved update job is available") from exc
    if plan.id != plan_id or plan.installation.installation_id != installation_id():
        raise ValueError("Update job belongs to a different installation or plan")
    # Local diagnostic messages may contain paths or exception details. Clients
    # receive typed facts and can localize phases, never raw journal messages.
    return UpdateJobView(
        id=plan_id, installation_id=plan.installation.installation_id, host=platform.node(),
        previous_version=plan.installation.installed_version, target_version=plan.target.version,
        channel=plan.channel, recorded_phase=status.phase, attempt=status.attempt,
        mutation_started=status.mutation_started, updated_at=status.updated_at,
    )


async def _settled_thread(function: Callable[_Args, _Result], *args: _Args.args,
                          **kwargs: _Args.kwargs) -> _Result:
    """Retain the caller's preparation leases until native work actually exits."""
    work = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        # Repeated request/shutdown cancellations must not cancel the wrapper
        # Future and release its leases while the underlying thread still runs.
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not work.cancelled():
            work.exception()  # Retrieve any native failure; cancellation still wins.
        raise


class _Metadata(BaseModel):
    name: str
    version: str


class _Hashes(BaseModel):
    sha256: Sha256


class _Archive(BaseModel):
    hashes: _Hashes


class _Download(BaseModel):
    url: str
    archive_info: _Archive


class _Install(BaseModel):
    metadata: _Metadata
    download_info: _Download


class _Report(BaseModel):
    model_config = ConfigDict(strict=True)
    version: str
    install: list[_Install] = Field(max_length=500)


def _pip(python: str, *arguments: str) -> None:
    result = subprocess.run([python, "-I", "-m", "pip", "--disable-pip-version-check", "--no-input", *arguments],
                            capture_output=True, timeout=900)
    if result.returncode:
        # Index URLs and resolver output may contain credentials. Do not put them
        # in the durable job or the WebUI response.
        raise ValueError("Could not prepare the pip transaction; check the installation's package-manager configuration")


def _worker_archive(path: Path) -> str:
    sources = Path(__file__).parent
    # The frozen worker must not shadow the newly installed application when
    # handing off to its runtime. Keep only the copied stdlib helpers in this
    # private namespace; intentional application imports retain `nanobot`.
    modules = ("update_worker", "update_uv", "update_backup", "data_access", "installation_lock", "utils.atomic_file")
    with ZipFile(path, "x") as bundle:
        files = {"__main__.py": b"from _nanobot_update.update_worker import main\nmain()\n",
                 "_nanobot_update/__init__.py": b"", "_nanobot_update/utils/__init__.py": b""}
        for module in modules:
            name = module.replace(".", "/") + ".py"
            source = (sources / name).read_text(encoding="utf-8")
            for dependency in modules:
                source = source.replace(f"from nanobot.{dependency} import", f"from _nanobot_update.{dependency} import")
            files[f"_nanobot_update/{name}"] = source.encode("utf-8")
        for name, data in files.items():
            bundle.writestr(ZipInfo(name), data)
    path.chmod(0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stage_app(plan: UpdatePlan, app_wheel: Path, directory: Path) -> StagedWheel:
    wheels = directory / "wheels"
    wheels.mkdir(mode=0o700, exist_ok=True)
    artifact = plan.target.artifact
    app = StagedWheel(artifact.filename, "nanobot-ai", plan.target.version, artifact.size, artifact.sha256)
    destination = wheels / artifact.filename
    if not destination.exists():
        shutil.copyfile(app_wheel, destination)
    app.verify(wheels)
    with ZipFile(app_wheel) as archive:
        if plan.restart_profiles and "nanobot/update_restart.py" not in archive.namelist():
            raise ValueError("This target does not support protected gateway restart")
        metadata = BytesParser().parsebytes(archive.read(f"nanobot_ai-{app.version}.dist-info/METADATA"))
        available = {canonicalize_name(extra) for extra in metadata.get_all("Provides-Extra", [])}
        if not set(plan.extras or []).issubset(available):
            raise ValueError("The target release does not provide all confirmed extras")
    return app


def _prepare_wheels(plan: UpdatePlan, app_wheel: Path, directory: Path) -> tuple[StagedWheel, ...]:
    app = _stage_app(plan, app_wheel, directory)
    wheels = directory / "wheels"
    records = [app]
    python = plan.installation.python
    _pip(python, "check")
    with tempfile.TemporaryDirectory(prefix=".resolve-", dir=directory) as temporary:
        root = Path(temporary)
        report_path = root / "report.json"
        extras = f"[{','.join(plan.extras)}]" if plan.extras else ""
        _pip(python, "install", "--dry-run", "--only-binary=:all:", "--upgrade",
             "--upgrade-strategy", "only-if-needed", "--report", str(report_path),
             f"nanobot-ai{extras} @ {app_wheel.as_uri()}")
        if report_path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Package-manager report exceeds its size limit")
        report = _Report.model_validate_json(report_path.read_bytes())
        if report.version != "1":
            raise ValueError("Unsupported pip report version")
        requirements: list[str] = []
        dependencies: list[tuple[str, str, str, str]] = []
        for item in report.install:
            name = canonicalize_name(item.metadata.name)
            if name == "nanobot-ai":
                if (item.metadata.version, item.download_info.archive_info.hashes.sha256) != (app.version, app.sha256):
                    raise ValueError("Resolver changed the approved nanobot target")
                continue
            url = item.download_info.url
            if any(character.isspace() for character in url) or urlsplit(url).scheme not in {"https", "file"}:
                raise ValueError("An update dependency must be an exact HTTPS or local wheel")
            filename = Path(unquote(urlsplit(url).path)).name
            wheel_name, version, _, _ = parse_wheel_filename(filename)
            if (wheel_name, str(version)) != (name, item.metadata.version) or name == "pip":
                raise ValueError("Resolver returned an unsupported dependency artifact")
            digest = item.download_info.archive_info.hashes.sha256
            requirements.append(f"{name} @ {url} --hash=sha256:{digest}")
            dependencies.append((filename, name, str(version), digest))
        if requirements:
            request = root / "requirements.txt"
            request.write_text("\n".join(requirements), encoding="utf-8")
            request.chmod(0o600)
            downloads = root / "downloads"
            _pip(python, "download", "--no-deps", "--only-binary=:all:", "--require-hashes",
                 "--dest", str(downloads), "--requirement", str(request))
            for filename, name, version, digest in dependencies:
                path = downloads / filename
                record = StagedWheel(filename, name, version, path.stat().st_size, digest)
                record.verify(downloads)
                shutil.copyfile(path, wheels / filename)
                records.append(record)
    return tuple(records)


def _prepare_transaction(plan: UpdatePlan, app_wheel: Path, directory: Path) -> tuple[tuple[StagedWheel, ...], UVPreparation | None]:
    if plan.installation.owner != "uv":
        return _prepare_wheels(plan, app_wheel, directory), None
    executable = shutil.which("uv")
    if executable is None:
        raise ValueError("The installation's uv manager is not available")
    app = _stage_app(plan, app_wheel, directory)
    prepared = prepare_uv(environment=plan.installation.environment, python=plan.installation.python,
                          receipt_sha256=plan.installation.records["uv-receipt.toml"],
                          app_wheel=directory / "wheels" / app.filename, version=plan.target.version,
                          extras=plan.extras or [], directory=directory, executable=executable)
    return (app,), prepared


async def prepare_job(plan: UpdatePlan, feed: ReleaseFeed) -> Path:
    """Stage a supported job without granting execution or changing installed files."""
    if plan.installation.owner not in {"venv", "uv"} or plan.extras is None:
        raise ValueError("The worker requires a supported dedicated environment and explicitly confirmed extras")
    native_uv = plan.installation.owner == "uv"
    if native_uv and (os.name != "posix" or plan.installation.manager is None):
        raise ValueError("This uv installation needs its owner's external update workflow")
    if plan.owner_restriction:
        raise ValueError(plan.owner_restriction)
    if plan.action != "upgrade":
        raise ValueError("This executor prepares upgrades only; no-op, restart and downgrade need their own review")
    plan.validate_for(plan.installation)
    root = installation_state_dir(plan.installation.environment)
    with installation_access(environment=plan.installation.environment), file_access(root / "prepare.lock", exclusive=True):
        directory = root / "jobs" / plan.id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (directory / "request.json").exists():
            spec = WorkerSpec.read(directory)
            if spec.plan_sha256 != hashlib.sha256((plan.model_dump_json() + "\n").encode()).hexdigest():
                raise ValueError("This job already belongs to another approved plan")
            return directory
        if (directory / "status.json").exists() and JobStatus.read(directory).phase == Phase.PREPARING:
            # A crashed preparer may have left a native child writing staging.
            # Absence of its Python task/lease is not proof that child exited.
            raise ValueError("A previous preparation did not settle; inspect this job before retrying or cleaning it up")
        plan_path = directory / "plan.json"
        encoded = plan.model_dump_json() + "\n"
        if plan_path.exists() and plan_path.read_bytes() != encoded.encode():
            raise ValueError("This preparation already belongs to another reviewed plan")
        if not plan_path.exists():
            atomic_write_lines(plan_path, [plan.model_dump_json()])
            plan_path.chmod(0o600)
        JobStatus(plan.id, Phase.PREPARING, 0, False,
                  "Checking the installation and preparing the exact package", int(time.time())).save(directory)
        try:
            return await _prepare_inputs(plan, feed, root, directory, native_uv=native_uv)
        except asyncio.CancelledError:
            JobStatus(plan.id, Phase.PREPARATION_FAILED, 0, False,
                      "Preparation cancelled after its workers exited; no installation was started",
                      int(time.time())).save(directory)
            raise
        except Exception as exc:
            # Resolver/network exceptions can contain index credentials or paths.
            JobStatus(plan.id, Phase.PREPARATION_FAILED, 0, False,
                      f"Preparation failed ({type(exc).__name__}); no installation was started",
                      int(time.time())).save(directory)
            raise


async def _prepare_inputs(plan: UpdatePlan, feed: ReleaseFeed, root: Path, directory: Path,
                          *, native_uv: bool) -> Path:
    baseline = await _settled_thread(inspect_environment, plan.installation.python, uv=native_uv)
    if (baseline.environment, baseline.python, baseline.app_version, baseline.receipt_sha) != (
        plan.installation.environment, plan.installation.python, plan.installation.installed_version,
        plan.installation.records.get("pyvenv.cfg"),
    ) or any(plan.installation.records.get(name) != digest for name, digest in baseline.app_records.items()):
        raise ValueError("The installation changed since this plan was prepared")
    if native_uv and baseline.manager_receipt_sha != plan.installation.records.get("uv-receipt.toml"):
        raise ValueError("The uv installation settings changed since this plan was prepared")
    validate_profiles(plan.profiles, plan.installation.environment)
    JobStatus(plan.id, Phase.PREPARING, 0, False,
              "Downloading and verifying the reviewed package", int(time.time())).save(directory)
    app_wheel = await feed.download(plan.target, root / "downloads")
    JobStatus(plan.id, Phase.PREPARING, 0, False,
              "Preparing dependencies without changing the installation", int(time.time())).save(directory)
    records, uv = await _settled_thread(_prepare_transaction, plan, app_wheel, directory)
    current = await _settled_thread(inspect_environment, plan.installation.python, uv=native_uv)
    if current != baseline:
        raise ValueError("Installation changed while preparing the update")
    plan.validate_for(plan.installation)
    plan_path = directory / "plan.json"
    archive = directory / "worker.pyz"
    if archive.exists():
        archive.unlink()  # Only uncommitted staging: no request/approval exists yet.
    sources = [BackupSource(str(Path(baseline.environment) / "pyvenv.cfg"))]
    if native_uv:
        sources.append(BackupSource(str(Path(baseline.environment) / "uv-receipt.toml")))
    for profile in plan.profiles:
        receipt = profile.receipt
        sources.extend(profile_sources(home=Path(receipt.home), data_dir=Path(receipt.data_dir),
                                       config=Path(receipt.config), workspace=Path(receipt.workspace)))
        sources.append(BackupSource(str(root / "profiles" / f"{receipt.id}.json")))
    spec = WorkerSpec(plan.id, hashlib.sha256(plan_path.read_bytes()).hexdigest(), _worker_archive(archive),
                      baseline, plan.target.version, records, unique_sources(sources), tuple(plan.restart_profiles), uv)
    for path in [archive, *(directory / "wheels" / item.filename for item in records)]:
        path.chmod(0o600)
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())
    JobStatus(plan.id, Phase.READY, 0, False, "Files prepared; not installed", int(time.time())).save(directory)
    # The immutable request is the commit record: its presence means every
    # input and the initial status were flushed successfully.
    spec.save(directory)
    return directory


async def start_job(directory: Path, feed: ReleaseFeed, *, repair: bool = False) -> subprocess.Popen[bytes]:
    """Approve the exact staged job and start its detached worker.

    Not called by a client route yet. The caller must quiesce/stop old runtimes;
    the worker rechecks their leases before installation. Only explicitly saved
    background participants may be restarted, never an external supervisor.
    """
    spec = WorkerSpec.read(directory)
    plan = UpdatePlan.model_validate_json((directory / "plan.json").read_bytes())
    runtime_phases = {Phase.RESTARTING, Phase.ACTIVATING, Phase.FINALIZING, Phase.RESTART_FAILED}
    if JobStatus.read(directory).phase not in runtime_phases:
        await feed.revalidate(plan.target)
    with file_access(installation_state_dir(spec.baseline.environment) / "worker.lock", exclusive=True):
        state = JobStatus.read(directory)
        if state.phase in {Phase.INSTALLED, Phase.CANCELLED, Phase.SUCCEEDED}:
            raise ValueError("This job is already finished; it cannot be started again")
        restarting = state.phase in runtime_phases
        with (installation_access(updating=not restarting, allow_incomplete=restarting,
                                  environment=spec.baseline.environment),
              nullcontext() if state.phase == Phase.FINALIZING else
              data_access((source.path for source in spec.backup_sources), updating=not restarting, job_id=spec.plan_id)):
            if state.mutation_started and not repair:
                raise ValueError("This interrupted job requires an explicit repair confirmation")
            if repair and not state.mutation_started:
                raise ValueError("Repair is only available after this job began replacing files")
            check_environment(spec, repairing=state.mutation_started)
            if not restarting:
                require_stopped_profiles(plan.profiles, spec.baseline.environment)
            spec.verify_inputs(directory)
            now = int(time.time())
            if not repair:
                plan.validate_for(plan.installation, now=now)
            write_record(directory / "approval.json", {
                "plan_id": spec.plan_id, "request_sha256": hashlib.sha256((directory / "request.json").read_bytes()).hexdigest(),
                "created_at": now, "expires_at": now + 900 if repair else min(plan.expires_at, now + 900),
            })
    return _spawn_worker(directory, spec)


def start_handoff(directory: Path) -> subprocess.Popen[bytes]:
    """Confirm an exact prepared plan and detach before the requesting gateway exits.

    Unlike start_job (already-stopped installations/repair), release revalidation
    and cooperative shutdown run independently of the caller. No installation
    approval is written until that child exits and exclusive leases are held.
    This remains internal until busy-owner accounting and client consent are wired.
    """
    spec = WorkerSpec.read(directory)
    plan = UpdatePlan.model_validate_json((directory / "plan.json").read_bytes())
    root = installation_state_dir(spec.baseline.environment)
    with (file_access(root / "worker.lock", exclusive=True),
          file_access(root / "handoff-child.lock", exclusive=True),
          installation_access(environment=spec.baseline.environment)):
        state = JobStatus.read(directory)
        if state.mutation_started or state.phase not in {
            Phase.READY, Phase.CHECKING, Phase.STOPPING, Phase.HANDOFF_FAILED,
        }:
            raise ValueError("Shutdown handoff requires an unmodified prepared job")
        now = int(time.time())
        plan.validate_for(plan.installation, now=now)
        spec.verify_inputs(directory)
        write_record(directory / "handoff-approval.json", {
            "plan_id": spec.plan_id, "request_sha256": hashlib.sha256((directory / "request.json").read_bytes()).hexdigest(),
            "created_at": now, "expires_at": min(plan.expires_at, now + 900),
        })
        (directory / "approval.json").unlink(missing_ok=True)
        JobStatus(spec.plan_id, Phase.CHECKING, state.attempt, False,
                  "Starting an independent update handoff", now).save(directory)
    # Release the ownership lease before spawning: a fast child must be able to
    # take it immediately. Duplicate launches still serialize in run_job.
    try:
        return _spawn_worker(directory, spec, handoff=True)
    except OSError:
        with file_access(root / "worker.lock", exclusive=True):
            state = JobStatus.read(directory)
            if state.phase == Phase.CHECKING:
                JobStatus(spec.plan_id, Phase.HANDOFF_FAILED, state.attempt, False,
                          "Could not start the independent update process; no shutdown was requested",
                          int(time.time())).save(directory)
        raise


def _spawn_worker(directory: Path, spec: WorkerSpec, *, handoff: bool = False) -> subprocess.Popen[bytes]:
    command = [spec.baseline.base_python, "-I", str(directory / "worker.pyz")]
    command.extend(["--handoff", str(directory)] if handoff else [str(directory)])
    if os.name == "nt":
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                cwd=directory, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            cwd=directory, start_new_session=True)
