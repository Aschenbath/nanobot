"""Prepare exact offline jobs and hand them to the standalone pip worker.

This is the executor's internal Interface, not an authenticated WebUI mutation.
The caller still owns task shutdown, data backup and supervisor/restart handoff.
Until that caller is integrated, CLI/UI cannot start these jobs.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile, ZipInfo

from packaging.utils import canonicalize_name, parse_wheel_filename
from pydantic import BaseModel, ConfigDict, Field

from nanobot.installation_lock import file_access, installation_access, installation_state_dir
from nanobot.release_feed import ReleaseFeed
from nanobot.releases import Sha256
from nanobot.update_plan import UpdatePlan
from nanobot.update_profiles import require_stopped_profiles, validate_profiles
from nanobot.update_worker import (
    JobStatus,
    Phase,
    StagedWheel,
    WorkerSpec,
    check_environment,
    inspect_environment,
    write_record,
)
from nanobot.utils.atomic_file import atomic_write_lines


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
    with ZipFile(path, "x") as bundle:
        files = {"__main__.py": b"from nanobot.update_worker import main\nmain()\n",
                 "nanobot/__init__.py": b"", "nanobot/utils/__init__.py": b""}
        for name in ("update_worker.py", "installation_lock.py", "utils/atomic_file.py"):
            files[f"nanobot/{name}"] = (sources / name).read_bytes()
        for name, data in files.items():
            bundle.writestr(ZipInfo(name), data)
    path.chmod(0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prepare_wheels(plan: UpdatePlan, app_wheel: Path, directory: Path) -> tuple[StagedWheel, ...]:
    wheels = directory / "wheels"
    wheels.mkdir(mode=0o700, exist_ok=True)
    artifact = plan.target.artifact
    app = StagedWheel(artifact.filename, "nanobot-ai", plan.target.version, artifact.size, artifact.sha256)
    destination = wheels / artifact.filename
    if not destination.exists():
        shutil.copyfile(app_wheel, destination)
    app.verify(wheels)
    with ZipFile(app_wheel) as archive:
        metadata = BytesParser().parsebytes(archive.read(f"nanobot_ai-{app.version}.dist-info/METADATA"))
        available = {canonicalize_name(extra) for extra in metadata.get_all("Provides-Extra", [])}
        if not set(plan.extras or []).issubset(available):
            raise ValueError("The target release does not provide all confirmed extras")
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


async def prepare_job(plan: UpdatePlan, feed: ReleaseFeed) -> Path:
    """Stage a plain-venv job without granting execution or changing installed files."""
    if plan.installation.owner != "venv" or plan.extras is None:
        raise ValueError("The pip worker requires a dedicated venv and explicitly confirmed extras")
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
        baseline = await asyncio.to_thread(inspect_environment, plan.installation.python)
        if (baseline.environment, baseline.python, baseline.app_version, baseline.receipt_sha) != (
            plan.installation.environment, plan.installation.python, plan.installation.installed_version,
            plan.installation.records.get("pyvenv.cfg"),
        ) or any(plan.installation.records.get(name) != digest for name, digest in baseline.app_records.items()):
            raise ValueError("The installation changed since this plan was prepared")
        validate_profiles(plan.profiles, plan.installation.environment)
        app_wheel = await feed.download(plan.target, root / "downloads")
        preparing = asyncio.create_task(asyncio.to_thread(_prepare_wheels, plan, app_wheel, directory))
        try:
            records = await asyncio.shield(preparing)
        except asyncio.CancelledError:
            # Closing a browser/cancelling its request must not release the staging
            # lease while the package-manager subprocess still writes this job.
            try:
                await preparing
            finally:
                raise
        current = await asyncio.to_thread(inspect_environment, plan.installation.python)
        if current != baseline:
            raise ValueError("Installation changed while preparing the update")
        plan.validate_for(plan.installation)
        plan_path = directory / "plan.json"
        atomic_write_lines(plan_path, [plan.model_dump_json()])
        plan_path.chmod(0o600)
        archive = directory / "worker.pyz"
        if archive.exists():
            archive.unlink()  # Only uncommitted staging: no request/approval exists yet.
        spec = WorkerSpec(plan.id, hashlib.sha256(plan_path.read_bytes()).hexdigest(), _worker_archive(archive),
                          baseline, plan.target.version, records)
        for path in [archive, *(directory / "wheels" / item.filename for item in records)]:
            path.chmod(0o600)
            with path.open("r+b") as stream:
                os.fsync(stream.fileno())
        JobStatus(plan.id, Phase.READY, 0, False, "Files prepared; not installed", int(time.time())).save(directory)
        # The immutable request is the commit record: its presence means every
        # input and the initial status were flushed successfully.
        write_record(directory / "request.json", {"schema_version": 1, **asdict(spec)})
        return directory


async def start_job(directory: Path, feed: ReleaseFeed, *, repair: bool = False) -> subprocess.Popen[bytes]:
    """Approve the exact staged job and start its detached worker.

    Not called by a client route yet. Runtime quiescence is enforced again in the
    worker; this call never stops gateways or establishes backup/restart approval.
    """
    spec = WorkerSpec.read(directory)
    plan = UpdatePlan.model_validate_json((directory / "plan.json").read_bytes())
    await feed.revalidate(plan.target)
    with file_access(installation_state_dir(spec.baseline.environment) / "worker.lock", exclusive=True):
        with installation_access(updating=True, environment=spec.baseline.environment):
            state = JobStatus.read(directory)
            if state.phase in {Phase.INSTALLED, Phase.CANCELLED}:
                raise ValueError("This job is already finished; it cannot be started again")
            if state.mutation_started and not repair:
                raise ValueError("This interrupted job requires an explicit repair confirmation")
            if repair and not state.mutation_started:
                raise ValueError("Repair is only available after this job began replacing files")
            check_environment(spec, repairing=state.mutation_started)
            require_stopped_profiles(plan.profiles, spec.baseline.environment)
            for wheel in spec.wheels:
                wheel.verify(directory / "wheels")
            now = int(time.time())
            if not repair:
                plan.validate_for(plan.installation, now=now)
            write_record(directory / "approval.json", {
                "plan_id": spec.plan_id, "request_sha256": hashlib.sha256((directory / "request.json").read_bytes()).hexdigest(),
                "created_at": now, "expires_at": now + 900 if repair else min(plan.expires_at, now + 900),
            })
    command = [spec.baseline.base_python, "-I", str(directory / "worker.pyz"), str(directory)]
    if os.name == "nt":
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                cwd=directory, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            cwd=directory, start_new_session=True)
