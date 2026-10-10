# Inspecting an update

The update work on this branch is a draft. Published stable installations continue
to use their existing package manager. Preview publication and the complete
stop/install/restart handoff are not enabled by these commands.

## Review an exact release

```bash
nanobot update --plan
nanobot update --plan --channel preview
```

Planning checks the running installation and fresh interpreter metadata, selects
a compatible prebuilt wheel, and saves an exact plan outside the package directory.
The report names the installation owner, installed and running versions, target
version, download size and hash, and whether this would be an upgrade, downgrade,
restart or no-op. It does not install anything, stop a task or restart a gateway.
It can write a private plan and release-metadata cache; it is not a zero-write command.

Plans expire after fifteen minutes. Revalidation rejects a changed interpreter
environment, version or ownership receipt, an expired plan, a withdrawn candidate,
or changed artifact bytes. It checks the version already in the plan rather than
selecting a new latest version. These checks are prerequisites for execution, not
evidence that execution or recovery has completed.

Stable metadata comes from PyPI. The complete five-platform wheel set and sdist must
be present and not yanked; unsupported platforms do not silently build from source.
Preview metadata uses the protected `release-index` catalogue of `HKUDS/nanobot`.
Until that catalogue and its approved releases are published, preview planning fails
with a network/not-available result. It does not install a Git checkout as a fallback.

The persisted catalogue rejects older generations and changed data under the same
generation. A cached catalogue cannot turn an unsuccessful network check into a fresh
result. Downloads only follow approved HTTPS hosts, use the existing DNS-pinning
transport, and check size, checksum and embedded wheel identity before staging.
An existing staged artifact is verified, never silently overwritten.

`--plan` is separate from the draft's older `--check`, release update and `--dev`
paths. Those execution paths do not consume these plans yet. Do not describe them
as implementing exact-plan consent, durable jobs, coordinated runtime maintenance,
backup or post-restart verification until the execution handoff is integrated and
tested. A plan also does not authorize bypassing uv/pipx, Docker, Desktop or an
external supervisor; the installation owner remains responsible for its files.

## Runtime coordination in this draft

The `nanobot` CLI entrypoint now holds a shared installation lease while running. Profiles using
the same real Python environment and OS account share it, including when launched
with different `--home` values or through a symlink. The updater requires an exclusive
lease and refuses to change package files while these runtimes are still using them.
Startup refuses while that lease is held or an interrupted-install marker remains.
An OS lock, not a PID file, owns the lease; a process crash releases it without
deleting or replacing the lock file.

This alone is not a complete maintenance handoff. In particular, the
older in-process WebUI updater cannot replace its own live gateway: it now reports
that the installation is in use instead of performing that unsafe operation. The
detached owner described below is still internal: client consent and remaining
background-writer accounting must be connected before enabling the finished in-app
install flow. Updates do not kill active tasks.

Leases protect cooperating entrypoints in the same account. They do not discover
older runtimes without this mechanism, arbitrary Python imports, another OS user's
processes or unrecognized supervisors. Such installations must not be advertised
as eligible for unattended/coordinated updates merely because a lease is available.

### Interactive channel connections

Settings registers channel-owned setup work with the same runtime admission gate.
A QR/browser response does not make a pending connection idle. Built-in Feishu,
WeChat, WhatsApp and Linear connectors expose their pending work and close it during
shutdown; settings retains ownership through cleanup and after a failed close.
Extensions without this lifecycle contract keep their existing connection behavior,
but cannot claim to be idle for coordinated updates while loaded.

WhatsApp's native worker must actually return before its temporary database is
promoted or removed. A shutdown timeout leaves the worker and files owned; retry
cleanup instead of interpreting a cancelled Python wrapper as a stopped native
thread. Linear releases its listener only after request threads, including webhook
writes, finish. Abandoned WhatsApp and Linear setup sessions initiate cleanup at
expiry without requiring another browser poll. Expiry or a cleanup timeout alone
does not establish that a remaining writer stopped.

### Model provider sign-in

The settings registry owns OpenAI Codex, GitHub Copilot and xAI sign-in workers
before starting them. Cancelling, expiring or replacing a sign-in removes its public
flow, but keeps it busy until its workers actually exit. A failed sign-in start
also retains ownership of any late worker. Shutdown signals cancellation and waits
for workers, callback requests and expiry timers to finish; cancelling the shutdown
awaiter does not release their ownership. Native network timeouts may delay exit.

Codex uses the SDK's public token-storage interface to reject saves after cancellation
or expiry, without moving existing credentials. This is not a new authorization
method and does not require existing users to sign in again.

These guards complement settings-request activity and actual gateway process exit.
They do not establish that every external integration writer is accounted for,
and do not enable the internal updater in the UI.

### Known gateway profiles

Gateway startup now records an installation-scoped profile receipt outside the package
being replaced. It identifies the exact interpreter/package version, selected home,
actual data directory, config and workspace paths, instance state/log paths, launch mode
and process identity. Config contents,
credentials and arbitrary environment variables are not copied into that record.
Receipts remain after exit so stopped profiles are included when planning future backups.

`--plan` includes these known profiles. A PID by itself is not liveness evidence: the
existing native process identity and runtime claim must match. A profile reused by another
installation is unconfirmed, not idle. A new or restarted profile invalidates the plan.
The executor may accept the reviewed gateways having stopped, but not a replacement
instance or an unconfirmed owner. Recording a foreground launch does not identify its
external supervisor or authorize terminating it.

This is an inventory of cooperating gateways, not discovery of all processes or a backup.
It does not authorize copying whole user workspaces. Older installations, other accounts,
external supervisors and standalone imports still require their own ownership checks.

## Exact offline executor (internal, not enabled in the UI)

`update_jobs.prepare_job` now prepares an immutable plain-venv transaction. It requires
explicitly confirmed extras (including an explicit empty list for the base package),
refuses unsupported installation owners, and compares fresh interpreter and installed
metadata with the approved plan. It asks pip for a dry-run report, fetches the exact
required dependency wheels with hashes, and stages those alongside the verified
nanobot wheel. There is no source build or global package-manager fallback. Pip's
configured package sources remain its trust policy during preparation; sensitive
resolver URLs stay in a private temporary directory and are not put in the journal.

Staging alone does not authorize execution. Starting a job revalidates the same release,
checks the current environment and writes a short-lived approval bound to the exact
request. The immutable worker zipapp uses only Python's standard library and lives
outside the files being replaced. Its coordinator runs with the base interpreter;
its dedicated pip CLI child holds the installation lease itself. Killing the coordinator
therefore cannot unlock a still-running installer. Installation is offline, from only
the staged wheels, with pip's destination/configuration overrides disabled.

The journal records the attempt, phase, whether mutation began, and a bounded message.
After pip exits, a fresh interpreter must import the target from the correct environment;
application files, including bundled WebUI/TUI assets, must match the approved wheel,
and `pip check` must pass. Without selected restart participants the result is
**installed**, not “gateway updated”. With explicitly selected background participants,
the worker starts the installed runtime under protected maintenance, verifies its
identity and readiness, then activates it. Only verified activation is **succeeded**;
a failed restart retains the recovery state and does not report success.

A stopped job can be cancelled only before mutation. Failed/interrupted mutation keeps
its startup fence and exact inputs. Explicit repair rechecks eligibility and cannot
overwrite a different version installed by another updater. Within a valid approval,
the copied worker can retry or show `--status <job-directory>` without importing a
broken nanobot installation. After expiry it refuses execution: a control-plane approval
must be renewed for the same reviewed job. There is not yet a finished public recovery
CLI for the case where that control plane is itself unavailable.

### Detached shutdown and installation

`update_jobs.start_handoff` confirms an exact prepared job and starts a detached owner
using the base Python. The initiating request can return; the gateway does not await
its own exit. Shutdown consent is separate from permission to replace package files.

The owner starts a short-lived child in the old installation. That child takes a shared
installation lease **before** importing application code, rechecks the exact release
and installation, closes the release client, then requests cooperative shutdown only
from the plan's selected idle gateways. All selected gateways must prepare before any
stop is committed. It waits for actual process exit, not just an inactive receipt.
The parent waits for this preflight child to exit too, obtains exclusive installation
and data leases, and only then grants installation approval and runs the offline worker.

The saved phases distinguish checking, stopping and a failed handoff from package
mutation. A shutdown commitment is not undone by a later failure: review the saved
state before retrying. A surviving preflight child retains its lease even if its parent
crashes; it cannot install by itself, and another handoff cannot overwrite its progress.
Retrying an unmodified handoff requires fresh consent within the exact plan's lifetime.
After package mutation starts, use the explicit repair path, not shutdown again.

These internal paths are verified with real detached/pip processes and disposable
application wheels, including dependency upgrades, caller exit, coordinator loss and
repair. Preflight/restart routing fixtures do not constitute a complete application
upgrade E2E. Actual isolated gateway stop and protected restart have separate tests.
Remaining background-writer accounting, uv/pipx execution, authenticated UI/CLI consent
and reconnection, and native platform acceptance are not complete. No public button
or CLI flag starts this new handoff yet.
