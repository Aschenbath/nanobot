"""Own exact-plan preparation independently of a client connection.

This is staging, not installation consent. Durable status precedes acknowledgement;
repeating an ID only reads its existing job. No phase automatically retries work.
The process lease coordinates controllers in gateways sharing this installation.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import ExitStack

from pydantic import field_validator

from nanobot.installation_lock import (
    file_access,
    installation_access,
    installation_id,
    installation_state_dir,
)
from nanobot.release_feed import ReleaseFeed
from nanobot.releases import ReleaseRecord
from nanobot.update_jobs import (
    UpdateJobNotFoundError,
    UpdateJobView,
    prepare_job,
    read_update_job,
    settle_interrupted_preparation,
    validate_preparation,
)
from nanobot.update_plan import UpdatePlan, UpdatePlans
from nanobot.update_worker import JobStatus, Phase, read_record
from nanobot.utils.atomic_file import atomic_write_lines


class UpdateJobRequest(ReleaseRecord):
    id: str

    @field_validator("id")
    @classmethod
    def canonical_id(cls, value: str) -> str:
        return UpdatePlan.canonical_id(value)


class UpdateJobService:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._closing = False

    def has_pending_work(self) -> bool:
        return self._task is not None and not self._task.done()

    def prepare(self, plan_id: str) -> UpdateJobView:
        """Acknowledge one durable staging request; never install or restart."""
        if self._closing:
            raise RuntimeError("The update service is shutting down")
        loop = asyncio.get_running_loop()
        root = installation_state_dir()
        plan = UpdatePlans(root / "plans").load(plan_id)
        if plan.installation.installation_id != installation_id():
            raise ValueError("The plan belongs to another installation")
        try:
            return read_update_job(plan_id)
        except UpdateJobNotFoundError:
            pass
        # Acquire synchronously before acknowledging or starting a task. Another
        # profile may return this job's journal, but cannot start competing work.
        leases = ExitStack()
        try:
            leases.enter_context(file_access(root / "preparation-controller.lock", exclusive=True))
            leases.enter_context(installation_access())
            validate_preparation(plan)
            directory = root / "jobs" / plan_id
            if directory.is_symlink():
                raise ValueError("Invalid update job directory")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            plan_path = directory / "plan.json"
            if plan_path.exists():
                if UpdatePlan.model_validate(read_record(plan_path)) != plan:
                    raise ValueError("The job belongs to another plan")
            else:
                atomic_write_lines(plan_path, [plan.model_dump_json()])
                plan_path.chmod(0o600)
            # Recheck after taking the lease: a second process might have
            # finished between the first lookup and acquisition.
            if (directory / "status.json").exists():
                return read_update_job(plan_id)
            JobStatus(plan_id, Phase.PREPARATION_REQUESTED, 0, False,
                      "Preparation requested; no installation approved", int(time.time())).save(directory)
            result = read_update_job(plan_id)
            owned = leases.pop_all()
            self._task = loop.create_task(self._prepare(plan, owned), name="nanobot-update-prepare")
            # Also release if loop shutdown cancels before the coroutine enters.
            self._task.add_done_callback(lambda _: owned.close())
            return result
        finally:
            leases.close()

    async def _prepare(self, plan: UpdatePlan, leases: ExitStack) -> None:
        root = installation_state_dir()
        directory = root / "jobs" / plan.id
        with leases:
            try:
                async with ReleaseFeed(root / "releases") as feed:
                    # Revalidate the reviewed target, never resolve `latest`.
                    # prepare_job probes the actual environment under native
                    # child-owned leases before downloads or dependency work.
                    await feed.revalidate(plan.target)
                    await prepare_job(plan, feed)
            except (Exception, asyncio.CancelledError) as exc:
                # Raw errors can include private index URLs. Cancellation is
                # recorded only after prepare_job has settled native work.
                # A cancellation while closing the HTTP client cannot undo the
                # durable staging commit which prepare_job already completed.
                if not (directory / "request.json").exists():
                    JobStatus(plan.id, Phase.PREPARATION_FAILED, 0, False,
                              f"Preparation stopped ({type(exc).__name__}); no installation was started",
                              int(time.time())).save(directory)

    def settle(self, plan_id: str) -> UpdateJobView:
        """Explicitly settle abandoned staging; no retry, deletion or installation."""
        job = read_update_job(plan_id)
        root = installation_state_dir()
        directory = root / "jobs" / plan_id
        with file_access(root / "preparation-controller.lock", exclusive=True):
            if job.recorded_phase == Phase.PREPARATION_REQUESTED:
                # No native child can start before PREPARING is persisted.
                with file_access(root / "prepare.lock", exclusive=True):
                    state = JobStatus.read(directory)
                    if state.phase != Phase.PREPARATION_REQUESTED or state.mutation_started or (directory / "request.json").exists():
                        raise ValueError("The preparation state changed")
                    JobStatus(plan_id, Phase.PREPARATION_FAILED, 0, False,
                              "Interrupted request settled; no installation was started", int(time.time())).save(directory)
            elif job.recorded_phase == Phase.PREPARING:
                settle_interrupted_preparation(directory)
            else:
                raise ValueError("Only an interrupted preparation can be settled")
        return read_update_job(plan_id)

    async def close(self) -> None:
        self._closing = True
        if self._task is not None:
            # Browser requests never own this task. Shutdown must also wait for
            # the real preparation, not release ownership on cancellation.
            while not self._task.done():
                try:
                    await asyncio.shield(self._task)
                except asyncio.CancelledError:
                    continue
            self._task.result()
