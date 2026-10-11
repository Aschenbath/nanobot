"""Old-runtime preflight for the immutable, detached update owner.

Entered by the frozen worker only after taking this installation's shared lease.
This child must exit before the base-Python parent approves package mutation.
No client route calls it, and it never launches an installer itself.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from nanobot.installation_lock import installation_state_dir
from nanobot.release_feed import ReleaseFeed
from nanobot.update_plan import UpdatePlan
from nanobot.update_stop import stop_reviewed_gateways
from nanobot.update_worker import (
    JobStatus,
    Phase,
    WorkerSpec,
    check_environment,
    current_environment,
)


async def _prepare(directory: Path) -> None:
    spec = WorkerSpec.read(directory)
    plan = UpdatePlan.model_validate_json((directory / "plan.json").read_bytes())
    status = JobStatus.read(directory)
    if status.phase != Phase.CHECKING or status.mutation_started:
        raise ValueError("Shutdown preflight requires an unchanged prepared job")
    try:
        plan.validate_for(plan.installation)
        # This process already owns the old runtime's lease, including startup
        # and atexit. Do not create an unprotected nested interpreter here.
        check_environment(spec, repairing=False, current=current_environment(uv=spec.uv is not None))
        async with ReleaseFeed(installation_state_dir(spec.baseline.environment) / "feed") as feed:
            await feed.revalidate(plan.target)
        plan.validate_for(plan.installation)
        JobStatus(spec.plan_id, Phase.STOPPING, status.attempt, False,
                  "Waiting for the reviewed idle gateways to exit normally", int(time.time())).save(directory)
        await stop_reviewed_gateways(directory)
    except Exception:
        current = JobStatus.read(directory)
        message = (
            "Release or installation revalidation failed; gateway shutdown was not requested"
            if current.phase == Phase.CHECKING else
            "Gateway shutdown did not complete; review the saved shutdown state before retrying"
        )
        JobStatus(spec.plan_id, Phase.HANDOFF_FAILED, status.attempt, False, message, int(time.time())).save(directory)
        raise


def prepare_handoff(directory: Path) -> None:
    """Revalidate and cooperatively stop; installation remains unapproved."""
    asyncio.run(_prepare(directory))
