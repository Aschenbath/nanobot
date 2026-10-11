"""Authenticated, refresh-safe review and job reads; no installer or server process runs."""

import asyncio
import json
import os
import platform
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from packaging.tags import Tag
from websockets.datastructures import Headers
from websockets.http11 import Request

from nanobot import update_plan
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.runtime import WebSocketConfig
from nanobot.config.home import get_selected_home_path, set_home_path
from nanobot.installation_lock import (
    InstallationBusyError,
    installation_access,
    installation_state_dir,
    pending_update_path,
)
from nanobot.release_feed import ReleaseFeed
from nanobot.releases import release_filenames
from nanobot.update_manager import ManagerPolicy
from nanobot.update_plan import InstallationSnapshot, UpdatePlans
from nanobot.update_profiles import ObservedProfile, ProfileLaunch, ProfileReceipt
from nanobot.update_worker import JobStatus, Phase
from nanobot.webui.client_contract import webui_contract
from nanobot.webui.gateway_services import build_gateway_services

CREATE = "/api/settings/nanobot-update/plan/create"
READ = "/api/settings/nanobot-update/plan"
JOB = "/api/settings/nanobot-update/job"


@pytest.fixture
def review_gateway(tmp_path, monkeypatch):
    account = tmp_path / "private account"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: account))
    previous_home = get_selected_home_path()
    set_home_path(tmp_path / "profile")
    environment = tmp_path / "tool environment"
    package = str(environment / "lib/nanobot/__init__.py")
    snapshot = InstallationSnapshot(
        environment=str(environment), python=str(environment / "bin/python"),
        package_path=package, running_package=package, installed_version="0.3.4",
        running_version="0.3.4", owner="uv", records={"uv-receipt.toml": "a" * 64},
        manager=ManagerPolicy(manager="uv", backend="uv", source="registry", extras=["webui"]),
    )
    observed = []
    for number, state in enumerate(("running", "stopped")):
        root = tmp_path / f"profile-{number}"
        receipt = ProfileReceipt(
            id=str(number + 1) * 64, run_id=uuid4().hex, environment=str(environment),
            python=snapshot.python, package=package, version="0.3.4", home=str(root),
            data_dir=str(root), config=str(root / "config.json"), workspace=str(root / "project"),
            state_path=str(root / "run/gateway.json"), log_path=str(root / "log/gateway.log"),
            port=19000 + number, launch_mode="background", pid=os.getpid() + 10000 + number,
            process_identity=f"fixture-{number}", active=state == "running", stop_protocol=1,
            launch=ProfileLaunch(home=str(root), config_path=None, workspace=None,
                                 working_directory=str(root), verbose=False, lifetime="explicit"),
        )
        observed.append(ObservedProfile(receipt=receipt, state=state))
    monkeypatch.setattr(update_plan, "inspect_installation", lambda: snapshot)
    monkeypatch.setattr(update_plan, "inspect_profiles", lambda _: list(observed))
    requests = []

    def release(request):
        requests.append(str(request.url))
        assert request.url.path == "/pypi/nanobot-ai/json"
        return httpx.Response(200, json={
            "info": {"name": "nanobot-ai", "version": "0.3.5", "requires_python": ">=3.11"},
            "urls": [{"filename": name, "size": 123, "digests": {"sha256": "b" * 64},
                      "url": f"https://files.pythonhosted.org/packages/{name}", "yanked": False}
                     for name in release_filenames("0.3.5") if name.endswith((".whl", ".tar.gz"))],
        })

    monkeypatch.setattr(update_plan, "ReleaseFeed", lambda directory: ReleaseFeed(
        directory, transport=httpx.MockTransport(release), supported_tags=[Tag("py3", "none", "win_amd64")],
    ))
    def gateway(surface="gateway"):
        return build_gateway_services(
            config=WebSocketConfig(), bus=MessageBus(), session_manager=None,
            static_dist_path=None, workspace_path=tmp_path / "workspace",
            default_restrict_to_workspace=True, config_path=tmp_path / "config.json",
            runtime_model_name=None, runtime_surface=surface, runtime_capabilities_overrides=None,
        )

    try:
        yield SimpleNamespace(gateway=gateway, snapshot=snapshot, observed=observed,
                              requests=requests, directory=installation_state_dir(environment))
    finally:
        set_home_path(previous_home)


def connection(*, local=False):
    host = "127.0.0.1:8765" if local else "remote.example"
    return SimpleNamespace(remote_address=("127.0.0.1" if local else "192.0.2.1", 12345),
                           request=Request("/ws", Headers({"Host": host, "Origin": f"http://{host}"})))


async def fetch(gateway, path, *, authorized=True):
    headers = Headers()
    if authorized:
        headers["Authorization"] = f"Bearer {gateway.tokens.issue_api_token(60)}"
    return await gateway.http.dispatch(connection(), Request(path, headers))


async def test_exact_review_survives_router_recreation_without_reselection_or_install(review_gateway):
    fixture = review_gateway
    gateway = fixture.gateway()
    denied = await fetch(gateway, READ + "?id=" + str(uuid4()), authorized=False)
    assert denied.status_code == 401
    denied = await fetch(gateway, CREATE)
    assert denied.status_code == 405
    response = await gateway.http.dispatch_webui_mutation(
        connection(), "settings.nanobot.update.plan", {"channel": "stable", "restart_running": True},
    )
    assert response.status_code == 200, response.body
    assert response.headers["Cache-Control"] == "no-store"
    payload = json.loads(response.body)
    review = payload["plan"]
    assert payload["installation_allowed"] is False
    assert payload["execution_available"] is False
    assert (review["running_version"], review["target_version"], review["owner"]) == ("0.3.4", "0.3.5", "uv")
    assert review["extras"] == ["webui"] and review["validity"] == "current"
    assert [(item["reviewed_state"], item["stop"], item["restart"]) for item in review["profiles"]] == [
        ("running", True, True), ("stopped", False, False),
    ]
    assert {"records", "manager", "python", "url", "target"}.isdisjoint(review)
    saved = UpdatePlans(fixture.directory / "plans").load(review["id"])
    assert saved.stop_profiles == saved.restart_profiles == [fixture.observed[0].receipt.id]
    assert not (fixture.directory / "jobs").exists()
    assert not (fixture.directory / "stop.json").exists()
    assert fixture.requests == ["https://pypi.org/pypi/nanobot-ai/json"]

    fresh_gateway = fixture.gateway()
    restored = await fetch(fresh_gateway, READ + "?id=" + review["id"])
    assert restored.headers["Cache-Control"] == "no-store"
    assert json.loads(restored.body) == payload
    assert len(fixture.requests) == 1, "Reading an existing review must not resolve a different latest target"
    assert "webui.update-plans.v1" in webui_contract()["capabilities"]


async def test_review_reports_profile_and_installation_changes_without_replacing_plan(review_gateway, monkeypatch):
    fixture = review_gateway
    gateway = fixture.gateway()
    created = await gateway.http.dispatch_webui_mutation(connection(local=True), "settings.nanobot.update.plan", {})
    assert created.status_code == 200
    payload = json.loads(created.body)
    assert payload["installation_allowed"] is True
    review = payload["plan"]
    assert not any(item["stop"] or item["restart"] for item in review["profiles"])
    saved_bytes = (fixture.directory / "plans" / f"{review['id']}.json").read_bytes()
    fixture.observed[0] = fixture.observed[0].model_copy(update={
        "receipt": fixture.observed[0].receipt.model_copy(update={"run_id": uuid4().hex}),
    })
    response = await fetch(gateway, READ + "?id=" + review["id"])
    assert json.loads(response.body)["plan"]["validity"] == "profiles_changed"
    monkeypatch.setattr(update_plan, "inspect_installation", lambda: fixture.snapshot.model_copy(update={
        "installed_version": "0.3.5", "records": {"uv-receipt.toml": "c" * 64},
    }))
    response = await fetch(gateway, READ + "?id=" + review["id"])
    changed = json.loads(response.body)["plan"]
    assert changed["validity"] == "installation_changed"
    assert changed["installed_version"] == "0.3.5"
    assert (fixture.directory / "plans" / f"{review['id']}.json").read_bytes() == saved_bytes


@pytest.mark.parametrize("payload", [
    {"channel": "dev"}, {"restart_running": "true"}, {"extras": ["--index-url"]},
    {"target": "https://untrusted.example/wheel"}, {"installation": "/another/tool"},
])
async def test_invalid_client_request_cannot_select_code_or_environment(review_gateway, payload):
    fixture = review_gateway
    response = await fixture.gateway().http.dispatch_webui_mutation(
        connection(), "settings.nanobot.update.plan", payload,
    )
    assert response.status_code == 400
    assert fixture.requests == []
    assert not (fixture.directory / "plans").exists()


async def test_expired_unknown_and_foreign_plans_have_explicit_results(review_gateway, monkeypatch):
    fixture = review_gateway
    gateway = fixture.gateway()
    response = await gateway.http.dispatch_webui_mutation(connection(), "settings.nanobot.update.plan", {})
    review = json.loads(response.body)["plan"]
    now = time.time()
    monkeypatch.setattr(update_plan.time, "time", lambda: now + 901)
    response = await fetch(gateway, READ + "?id=" + review["id"])
    assert json.loads(response.body)["plan"]["validity"] == "expired"
    missing = await fetch(gateway, READ + "?id=" + str(uuid4()))
    assert missing.status_code == 404
    traversal = await fetch(gateway, READ + "?id=../../config")
    assert traversal.status_code == 400
    other = fixture.snapshot.model_copy(update={"environment": str(fixture.directory / "other")})
    monkeypatch.setattr(update_plan, "inspect_installation", lambda: other)
    missing = await fetch(gateway, READ + "?id=" + review["id"])
    assert missing.status_code == 404


async def test_desktop_and_failed_discovery_do_not_claim_executable_or_current(review_gateway, monkeypatch):
    fixture = review_gateway
    desktop = await fixture.gateway("native").http.dispatch_webui_mutation(
        connection(), "settings.nanobot.update.plan", {},
    )
    assert desktop.status_code == 409 and fixture.requests == []

    def failed_probe():
        raise ValueError("https://user:secret@index.example/private")

    monkeypatch.setattr(update_plan, "inspect_installation", failed_probe)
    failed = await fixture.gateway().http.dispatch_webui_mutation(connection(), "settings.nanobot.update.plan", {})
    assert failed.status_code == 409
    assert b"secret" not in failed.body
    assert not (fixture.directory / "plans").exists()


async def test_environment_replaced_during_discovery_does_not_return_a_valid_review(review_gateway, monkeypatch):
    fixture = review_gateway
    calls = 0

    def changed_probe():
        nonlocal calls
        calls += 1
        if calls == 1:
            return fixture.snapshot
        return fixture.snapshot.model_copy(update={"installed_version": "0.3.6"})

    monkeypatch.setattr(update_plan, "inspect_installation", changed_probe)
    response = await fixture.gateway().http.dispatch_webui_mutation(
        connection(), "settings.nanobot.update.plan", {},
    )
    assert response.status_code == 409
    assert response.body == b"Could not verify an update plan for this installation. No update was started."
    assert calls == 2
    assert not (fixture.directory / "jobs").exists()


async def test_missing_interpreter_is_not_misreported_as_a_missing_plan(review_gateway, monkeypatch):
    fixture = review_gateway
    gateway = fixture.gateway()
    response = await gateway.http.dispatch_webui_mutation(connection(), "settings.nanobot.update.plan", {})
    plan_id = json.loads(response.body)["plan"]["id"]

    def removed_python():
        raise FileNotFoundError("/private/installation/bin/python")

    monkeypatch.setattr(update_plan, "inspect_installation", removed_python)
    failed = await fetch(gateway, READ + "?id=" + plan_id)
    assert failed.status_code == 409
    assert b"/private/installation" not in failed.body
    assert (fixture.directory / "plans" / f"{plan_id}.json").exists()


async def test_job_reads_survive_refresh_expiry_and_unavailable_installation(review_gateway, monkeypatch):
    fixture = review_gateway
    gateway = fixture.gateway()
    response = await gateway.http.dispatch_webui_mutation(connection(), "settings.nanobot.update.plan", {})
    plan_id = json.loads(response.body)["plan"]["id"]
    plan = UpdatePlans(fixture.directory / "plans").load(plan_id)
    directory = fixture.directory / "jobs" / plan_id
    directory.mkdir(parents=True)
    (directory / "plan.json").write_text(plan.model_dump_json())
    JobStatus(plan_id, Phase.PREPARING, 0, False, "private diagnostic", plan.created_at).save(directory)
    monkeypatch.setattr(sys, "prefix", fixture.snapshot.environment)

    def unavailable_probe():
        pytest.fail("Reading a saved job must not launch the changing installation")

    monkeypatch.setattr(update_plan, "inspect_installation", unavailable_probe)
    response = await fetch(gateway, JOB + "?id=" + plan_id, authorized=False)
    assert response.status_code == 401
    response = await fetch(gateway, JOB + "?id=" + plan_id)
    assert response.status_code == 200
    job = json.loads(response.body)["job"]
    assert job == {
        "id": plan_id, "installation_id": fixture.snapshot.installation_id,
        "host": platform.node(), "previous_version": "0.3.4",
        "target_version": "0.3.5", "channel": "stable", "recorded_phase": "preparing",
        "attempt": 0, "mutation_started": False, "updated_at": plan.created_at,
    }
    assert not (directory / "request.json").exists(), "An early journal must not require committed staging"
    assert "webui.update-jobs.read.v1" in webui_contract()["capabilities"]

    # A journal is recoverable without revalidating the expired plan or reading
    # package metadata; it is not permission to retry or proof a worker is live.
    monkeypatch.setattr(update_plan.time, "time", lambda: plan.expires_at + 1)
    JobStatus(plan_id, Phase.FAILED, 1, True, "https://user:secret@private.example", plan.expires_at).save(directory)
    pending = pending_update_path()
    pending.parent.mkdir(parents=True)
    pending.write_text("interrupted update")
    with installation_access(updating=True):
        response = await fetch(fixture.gateway(), JOB + "?id=" + plan_id)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    job = json.loads(response.body)["job"]
    assert job["recorded_phase"] == "failed" and job["mutation_started"] is True
    assert job["attempt"] == 1 and job["updated_at"] == plan.expires_at
    assert b"secret" not in response.body and b"private account" not in response.body
    assert {"message", "running", "can_retry", "python", "url", "profiles"}.isdisjoint(job)
    assert len(fixture.requests) == 1
    assert not (directory / "approval.json").exists()


async def test_job_reads_reject_missing_foreign_and_corrupt_records(review_gateway, monkeypatch):
    fixture = review_gateway
    gateway = fixture.gateway()
    response = await gateway.http.dispatch_webui_mutation(connection(), "settings.nanobot.update.plan", {})
    plan_id = json.loads(response.body)["plan"]["id"]
    plan = UpdatePlans(fixture.directory / "plans").load(plan_id)
    monkeypatch.setattr(sys, "prefix", fixture.snapshot.environment)
    for query in ("", "?id=../../config", "?id=not-a-uuid"):
        assert (await fetch(gateway, JOB + query)).status_code == 400
    assert (await fetch(gateway, JOB + "?id=" + plan_id)).status_code == 404
    directory = fixture.directory / "jobs" / plan_id
    directory.mkdir(parents=True)
    (directory / "plan.json").write_text(plan.model_dump_json())
    (directory / "status.json").write_text('{"private":"secret"}')
    response = await fetch(gateway, JOB + "?id=" + plan_id)
    assert response.status_code == 409 and b"secret" not in response.body
    assert response.headers["Cache-Control"] == "no-store"
    JobStatus(plan_id, Phase.READY, 0, False, "Prepared", plan.created_at).save(directory)
    foreign = plan.model_copy(update={"installation": fixture.snapshot.model_copy(
        update={"environment": str(fixture.directory / "another installation")},
    )})
    (directory / "plan.json").write_text(foreign.model_dump_json())
    assert (await fetch(gateway, JOB + "?id=" + plan_id)).status_code == 409
    (directory / "plan.json").write_text(plan.model_dump_json())
    assert (await fetch(fixture.gateway("native"), JOB + "?id=" + plan_id)).status_code == 409
    monkeypatch.setattr(sys, "prefix", str(fixture.directory / "another installation"))
    assert (await fetch(gateway, JOB + "?id=" + plan_id)).status_code == 404


@pytest.mark.parametrize("reading", [False, True])
async def test_cancelled_request_keeps_probe_lease_until_native_work_exits(review_gateway, monkeypatch, reading):
    fixture = review_gateway
    entered, release = threading.Event(), threading.Event()

    def native_probe():
        entered.set()
        assert release.wait(5), "The test did not settle its native probe"
        return fixture.snapshot

    monkeypatch.setattr(update_plan, "inspect_installation", native_probe)
    task = asyncio.create_task(update_plan.read_release_plan(str(uuid4())) if reading
                               else update_plan.preview_release_update("stable"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(InstallationBusyError):
            with installation_access(updating=True):
                pytest.fail("Native inspection outlived its installation lease")
        assert fixture.requests == []
    finally:
        release.set()
        # Wait for the probe's actual lease release before restoring Path.home.
        async with asyncio.timeout(5):
            while True:
                try:
                    with installation_access(updating=True):
                        break
                except InstallationBusyError:
                    await asyncio.sleep(0.01)


@pytest.mark.parametrize("problem", ["unknown", "supervisor", "extras"])
async def test_plan_cannot_invent_restart_ownership_or_replace_manager_extras(review_gateway, problem):
    fixture = review_gateway
    payload = {"restart_running": True}
    if problem == "unknown":
        fixture.observed[0] = fixture.observed[0].model_copy(update={"state": "unconfirmed"})
    elif problem == "supervisor":
        fixture.observed[0] = fixture.observed[0].model_copy(update={
            "receipt": fixture.observed[0].receipt.model_copy(update={"launch_mode": "foreground"}),
        })
    else:
        payload["extras"] = []
    response = await fixture.gateway().http.dispatch_webui_mutation(connection(), "settings.nanobot.update.plan", payload)
    assert response.status_code == 409
    assert not (fixture.directory / "plans").exists()
