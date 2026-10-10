"""Real offline pip jobs in disposable venvs; no user installation is mutated."""

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import threading
import time
import venv
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from packaging.tags import sys_tags

from nanobot.installation_lock import (
    InstallationBusyError,
    installation_access,
    pending_update_path,
)
from nanobot.release_feed import ReleaseFeed, ReleaseTarget
from nanobot.releases import PLATFORMS, ReleaseArtifact, release_filenames
from nanobot.update_jobs import prepare_job, start_job
from nanobot.update_plan import InstallationSnapshot, UpdatePlan
from nanobot.update_worker import (
    JobStatus,
    Phase,
    WorkerSpec,
    cancel_job,
    inspect_environment,
    write_record,
)


def wheel(directory, name, version, *, requires=(), platform="any", broken=False):
    package = name.replace("-", "_")
    dist = f"{package}-{version}.dist-info"
    files = {
        f"{package if name != 'nanobot-ai' else 'nanobot'}/__init__.py": f"__version__ = '{version}'\n".encode(),
        f"{dist}/METADATA": (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                             + "".join(f"Requires-Dist: {requirement}\n" for requirement in requires)).encode(),
        f"{dist}/WHEEL": f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-{platform}\n".encode(),
    }
    if name == "nanobot-ai":
        if broken:
            files["nanobot/__init__.py"] = b"raise RuntimeError('broken release fixture')\n"
        files["nanobot/web/dist/index.html"] = f"verified UI {version}".encode()
        files["nanobot/tui/bin/fixture"] = b"fixture, not a native execution claim"
    records = []
    for filename, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        records.append(f"{filename},sha256={digest},{len(data)}")
    files[f"{dist}/RECORD"] = ("\n".join(records) + f"\n{dist}/RECORD,,\n").encode()
    path = directory / f"{package}-{version}-py3-none-{platform}.whl"
    with ZipFile(path, "w") as archive:
        for filename, data in files.items():
            archive.writestr(filename, data)
    return path


@pytest.fixture
def installation(tmp_path, monkeypatch, request):
    state_home = tmp_path / "private home"
    state_home.mkdir()
    monkeypatch.setenv("HOME", str(state_home))
    monkeypatch.setenv("USERPROFILE", str(state_home))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    environment = tmp_path / "venv with spaces"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = str(environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(artifacts))
    for version in ("1.0.0", "2.0.0"):
        wheel(artifacts, "fixture-update-dep", version)
    previous = wheel(artifacts, "nanobot-ai", "0.3.4", requires=["fixture-update-dep==1.0.0"])
    subprocess.run([python, "-I", "-m", "pip", "install", "--disable-pip-version-check", str(previous)],
                   check=True, capture_output=True, timeout=60)
    platform = next(tag.platform for tag in sys_tags() if tag.platform in PLATFORMS.values())
    candidate = wheel(artifacts, "nanobot-ai", "0.3.5", requires=["fixture-update-dep>=2.0.0"], platform=platform,
                      broken=getattr(request, "param", False))
    baseline = inspect_environment(python)
    site = Path(subprocess.check_output([python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
    snapshot = InstallationSnapshot(
        environment=str(environment.resolve()), python=python, installed_version="0.3.4", running_version="0.3.4",
        package_path=str(site / "nanobot/__init__.py"), running_package=str(site / "nanobot/__init__.py"),
        owner="venv", records={"pyvenv.cfg": baseline.receipt_sha, **baseline.app_records},
    )
    target = ReleaseTarget(version="0.3.5", source="pypi",
                           url=f"https://files.pythonhosted.org/packages/{candidate.name}",
                           artifact=ReleaseArtifact(filename=candidate.name, size=candidate.stat().st_size,
                                                    sha256=hashlib.sha256(candidate.read_bytes()).hexdigest()))
    now = int(time.time())
    plan = UpdatePlan(id=str(uuid4()), installation=snapshot, target=target, extras=[], channel="stable",
                      created_at=now, expires_at=now + 900)
    requests = []

    def response(request):
        requests.append(str(request.url))
        if request.url.host == "files.pythonhosted.org":
            return httpx.Response(200, content=candidate.read_bytes())
        assert request.url.path == "/pypi/nanobot-ai/0.3.5/json", "execution re-resolved latest"
        files = []
        for filename in release_filenames("0.3.5"):
            if filename.endswith((".whl", ".tar.gz")):
                selected = filename == candidate.name
                files.append({"filename": filename, "size": target.artifact.size if selected else 1,
                              "url": f"https://files.pythonhosted.org/packages/{filename}", "yanked": False,
                              "digests": {"sha256": target.artifact.sha256 if selected else "a" * 64}})
        return httpx.Response(200, json={"info": {"name": "nanobot-ai", "version": "0.3.5", "requires_python": ">=3.11"}, "urls": files})

    return plan, candidate, site, requests, httpx.MockTransport(response)


async def wait_worker(process):
    code = await asyncio.to_thread(process.wait, 40)
    assert code == 0


@pytest.mark.asyncio
async def test_real_job_stages_then_installs_exact_app_and_dependency(installation, monkeypatch, tmp_path):
    plan, _, site, requests, transport = installation
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        directory = await prepare_job(plan, feed)
        assert inspect_environment(plan.installation.python).app_version == "0.3.4"
        assert not (directory / "approval.json").exists()
        spec = WorkerSpec.read(directory)
        assert {(item.name, item.version) for item in spec.wheels} == {
            ("nanobot-ai", "0.3.5"), ("fixture-update-dep", "2.0.0"),
        }
        assert await prepare_job(plan, feed) == directory
        # Installer configuration cannot redirect the actual write to another env.
        unintended = tmp_path / "not-the-approved-environment"
        monkeypatch.setenv("PIP_TARGET", str(unintended))
        await wait_worker(await start_job(directory, feed))
    status = JobStatus.read(directory)
    assert status.phase == Phase.INSTALLED and status.attempt == 1 and status.mutation_started
    assert "not yet verified" in status.message
    assert inspect_environment(plan.installation.python).app_version == "0.3.5"
    assert (site / "nanobot/web/dist/index.html").read_text() == "verified UI 0.3.5"
    assert not unintended.exists()
    assert not pending_update_path(plan.installation.environment).exists()
    # Recover the reachable crash gap between saving INSTALLED and clearing the
    # startup fence. The completed job must verify/clear it, not install again.
    write_record(pending_update_path(plan.installation.environment),
                 {"schema_version": 2, "job_id": plan.id, "version": "0.3.5"})
    result = subprocess.run([spec.baseline.base_python, "-I", str(directory / "worker.pyz"), str(directory)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert JobStatus.read(directory).attempt == 1
    assert not pending_update_path(plan.installation.environment).exists()
    assert requests and all("/0.3.5/" in url or "/packages/" in url for url in requests)


@pytest.mark.asyncio
async def test_changed_artifact_or_owner_receipt_never_installs(installation, tmp_path):
    plan, _, _, _, transport = installation
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        directory = await prepare_job(plan, feed)
        app = directory / "wheels" / plan.target.artifact.filename
        original = app.read_bytes()
        app.write_bytes(original + b"changed")
        with pytest.raises(ValueError, match="size"):
            await start_job(directory, feed)
        assert not pending_update_path(plan.installation.environment).exists()
        app.write_bytes(original)
        receipt = Path(plan.installation.environment) / "pyvenv.cfg"
        receipt.write_text(receipt.read_text() + "\n# changed by another owner\n")
        with pytest.raises(ValueError, match="ownership"):
            await start_job(directory, feed)
    assert inspect_environment(plan.installation.python).app_version == "0.3.4"


@pytest.mark.asyncio
async def test_stale_plan_is_rejected_before_staging(installation, tmp_path):
    plan, _, _, requests, transport = installation
    # A real environment change, not a hypothetical internal flag.
    receipt = Path(plan.installation.environment) / "pyvenv.cfg"
    receipt.write_text(receipt.read_text() + "\n# changed\n")
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        with pytest.raises(ValueError, match="changed since"):
            await prepare_job(plan, feed)
    assert not requests


@pytest.mark.asyncio
async def test_worker_survives_coordinator_loss_without_unlocking_live_pip(installation, tmp_path):
    plan, _, site, _, transport = installation
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        directory = await prepare_job(plan, feed)
        ready, release = tmp_path / "pip-ready", tmp_path / "pip-continue"
        pip_entry = site / "pip/__main__.py"
        original = pip_entry.read_text()
        # Hold the real pip CLI immediately before it runs; only the disposable
        # fixture's launcher is wrapped, and its original body still executes.
        wrapper = (
            f"from pathlib import Path\nimport os,time\nPath({str(ready)!r}).write_text(str(os.getpid()))\n"
            f"deadline=time.monotonic()+20\nwhile not Path({str(release)!r}).exists():\n"
            "    if time.monotonic()>deadline: raise SystemExit(99)\n    time.sleep(0.02)\n"
            f"exec(compile({original!r}, __file__, 'exec'))\n"
        )
        pip_entry.write_text(wrapper)
        process = await start_job(directory, feed)
        try:
            for _ in range(200):
                if ready.exists():
                    break
                assert process.poll() is None, JobStatus.read(directory)
                await asyncio.sleep(0.025)
            assert ready.exists()
            process.kill()
            process.wait(timeout=10)
            assert JobStatus.read(directory).phase == Phase.INSTALLING
            with pytest.raises(InstallationBusyError):
                with installation_access(updating=True, environment=plan.installation.environment):
                    pytest.fail("Live pip lost its installation lease with the coordinator")
            with pytest.raises(InstallationBusyError):
                await start_job(directory, feed, repair=True)
        finally:
            release.touch()
            if process.poll() is None:
                process.wait(timeout=30)
        # Wait for the actual pip lease to disappear, not just the dead parent's PID.
        for _ in range(400):
            try:
                with installation_access(updating=True, environment=plan.installation.environment):
                    break
            except InstallationBusyError:
                await asyncio.sleep(0.025)
        else:
            pytest.fail("Pip did not release its lease")
        assert inspect_environment(plan.installation.python).app_version == "0.3.5"
        pip_entry.write_text(original)
        (site / "nanobot/web/dist/index.html").unlink()
        with pytest.raises(ValueError, match="explicit repair"):
            await start_job(directory, feed)
        await wait_worker(await start_job(directory, feed, repair=True))
    assert JobStatus.read(directory).attempt == 2
    assert JobStatus.read(directory).phase == Phase.INSTALLED
    assert (site / "nanobot/web/dist/index.html").read_text() == "verified UI 0.3.5"


@pytest.mark.asyncio
async def test_prepared_worker_has_no_execution_authority(installation, tmp_path):
    plan, _, _, _, transport = installation
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        with pytest.raises(ValueError, match="confirmed extras"):
            await prepare_job(plan.model_copy(update={"extras": ["not-provided"]}), feed)
        directory = await prepare_job(plan, feed)
    spec = WorkerSpec.read(directory)
    result = subprocess.run([spec.baseline.base_python, "-I", str(directory / "worker.pyz"), str(directory)],
                            capture_output=True, timeout=20)
    assert result.returncode != 0
    assert inspect_environment(plan.installation.python).app_version == "0.3.4"
    assert not pending_update_path(plan.installation.environment).exists()
    assert JobStatus.read(directory).phase == Phase.FAILED
    assert not JobStatus.read(directory).mutation_started
    # A saved but expired approval cannot authorize a delayed launch.
    now = int(time.time())
    write_record(directory / "approval.json", {
        "plan_id": plan.id, "request_sha256": hashlib.sha256((directory / "request.json").read_bytes()).hexdigest(),
        "created_at": now - 1000, "expires_at": now - 100,
    })
    result = subprocess.run([spec.baseline.base_python, "-I", str(directory / "worker.pyz"), str(directory)],
                            capture_output=True, timeout=20)
    assert result.returncode != 0
    assert "expired" in JobStatus.read(directory).message
    cancel_job(directory)
    result = subprocess.run([spec.baseline.base_python, "-I", str(directory / "worker.pyz"), str(directory)],
                            capture_output=True, timeout=20)
    assert result.returncode != 0
    assert JobStatus.read(directory).phase == Phase.CANCELLED


@pytest.mark.asyncio
@pytest.mark.parametrize("installation", [True], indirect=True)
async def test_broken_release_keeps_recovery_record_and_never_overwrites_an_external_upgrade(installation, tmp_path):
    plan, candidate, _, _, transport = installation
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        directory = await prepare_job(plan, feed)
        process = await start_job(directory, feed)
        assert await asyncio.to_thread(process.wait, 40) != 0
        state = JobStatus.read(directory)
        assert state.phase == Phase.FAILED and state.mutation_started
        assert "does not import" in state.message
        assert pending_update_path(plan.installation.environment).exists()
        spec = WorkerSpec.read(directory)
        # The status command is usable even though importing nanobot now fails.
        result = subprocess.run([spec.baseline.base_python, "-I", str(directory / "worker.pyz"), "--status", str(directory)],
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0
        assert json.loads(result.stdout)["phase"] == "failed"
        with pytest.raises(ValueError, match="not cancellation"):
            cancel_job(directory)
        newer = wheel(candidate.parent, "nanobot-ai", "0.3.6", requires=["fixture-update-dep>=2.0.0"])
        subprocess.run([plan.installation.python, "-I", "-m", "pip", "install", "--disable-pip-version-check", str(newer)],
                       capture_output=True, check=True, timeout=30)
        with pytest.raises(ValueError, match="cannot overwrite"):
            await start_job(directory, feed, repair=True)
    assert inspect_environment(plan.installation.python).app_version == "0.3.6"


@pytest.mark.asyncio
async def test_cancelled_preparation_keeps_lease_until_package_manager_exits(installation, monkeypatch, tmp_path):
    from nanobot import update_jobs

    plan, _, _, _, transport = installation
    entered, finish = threading.Event(), threading.Event()
    original = update_jobs._prepare_wheels

    def held_preparation(*args):
        entered.set()
        assert finish.wait(20)
        return original(*args)

    monkeypatch.setattr(update_jobs, "_prepare_wheels", held_preparation)
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        preparing = asyncio.create_task(prepare_job(plan, feed))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            preparing.cancel()
            await asyncio.sleep(0)
            assert not preparing.done()
            with pytest.raises(BlockingIOError):
                await prepare_job(plan, feed)
        finally:
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await preparing
        directory = await prepare_job(plan, feed)
    assert JobStatus.read(directory).phase == Phase.READY
    assert inspect_environment(plan.installation.python).app_version == "0.3.4"
