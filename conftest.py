"""Cross-suite test infrastructure."""

from __future__ import annotations

import os
import ssl
import sys
from collections.abc import Iterator
from pathlib import Path
from threading import Thread
from uuid import uuid4

import certifi
import pytest
from loguru import logger


@pytest.fixture(autouse=True)
def _isolate_tokenizer_warmup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests deterministic and out of the user's tokenizer cache and network."""
    monkeypatch.setattr("nanobot.utils.token_encoding._encoding", None)
    monkeypatch.setattr("nanobot.utils.token_encoding._warmup_thread", Thread())


@pytest.fixture(autouse=True)
def _isolate_nanobot_log_activation() -> Iterator[None]:
    """Keep CLI log settings from leaking into later tests in the same process."""
    logger.enable("nanobot")
    try:
        yield
    finally:
        logger.enable("nanobot")


@pytest.fixture(autouse=True)
def _isolate_sessions_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Redirect session storage away from the real active config data directory.

    Session storage lives under the active runtime data root (outside the workspace,
    per ADR-0001), so without redirection tests would write into the real home.
    """
    data_root = tmp_path.parent / "session-data" / uuid4().hex
    runtime_root = data_root / "runtime"

    def runtime_subdir(name: str) -> Path:
        path = runtime_root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    monkeypatch.setattr(
        "nanobot.session.location.get_runtime_subdir",
        runtime_subdir,
    )
    monkeypatch.setattr("nanobot.webui.history.get_webui_dir", lambda: runtime_subdir("webui"))
    yield


@pytest.fixture(autouse=True)
def _isolate_llm_usage_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    import nanobot.llm_usage.service as llm_usage

    stores: dict[Path, llm_usage.LLMUsageStore] = {}
    monkeypatch.setattr(llm_usage, "_STORES", stores)
    try:
        yield
    finally:
        for store in stores.values():
            store.close()


@pytest.fixture(autouse=True)
def _isolate_star_prompt_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep WebUI completion events out of the user's invitation state and lock."""
    webui_dir = tmp_path.parent / "star-prompt-webui" / uuid4().hex

    def get_webui_dir() -> Path:
        webui_dir.mkdir(parents=True, exist_ok=True)
        return webui_dir

    monkeypatch.setattr("nanobot.webui.star_prompt.get_webui_dir", get_webui_dir)


@pytest.fixture(autouse=True)
def _isolate_pairing_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep channel pairing tests out of the user's active pairing store."""
    pairing_path = tmp_path / "pairing.json"
    monkeypatch.setattr(
        "nanobot.pairing.store._store_path",
        lambda: pairing_path,
    )


@pytest.fixture(scope="session", autouse=True)
def _use_windows_system_ca_for_default_http_clients() -> Iterator[None]:
    """Avoid reparsing certifi's CA bundle for every offline HTTP client.

    Loading certifi takes roughly 0.7 seconds per client on Windows. The test
    suite constructs hundreds of clients while mocking their I/O. System roots
    preserve certificate verification for accidental local requests; explicit
    ``cafile``, ``capath``, and ``cadata`` arguments still use the real loader.
    """
    if sys.platform != "win32":
        yield
        return

    original = ssl.create_default_context
    certifi_path = os.path.normcase(os.path.abspath(certifi.where()))

    def create_default_context(
        purpose: ssl.Purpose = ssl.Purpose.SERVER_AUTH,
        *,
        cafile: str | None = None,
        capath: str | None = None,
        cadata: str | bytes | None = None,
    ) -> ssl.SSLContext:
        requested_path = os.path.normcase(os.path.abspath(cafile)) if cafile else None
        if requested_path == certifi_path and capath is None and cadata is None:
            return original(purpose)
        return original(
            purpose,
            cafile=cafile,
            capath=capath,
            cadata=cadata,
        )

    ssl.create_default_context = create_default_context
    try:
        yield
    finally:
        ssl.create_default_context = original


@pytest.fixture
def create_symlink():
    """Create real symlinks, skipping only Windows' missing-privilege error."""
    def create(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except OSError as exc:
            if sys.platform == "win32" and getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows symlink creation requires Developer Mode or privilege")
            raise

    return create


class DisplayHistoryFixture:
    """Explicit offline setup plus snapshot reads for history projection tests."""

    def __init__(self, workspace: Path) -> None:
        from nanobot.session.manager import SessionManager

        self.sessions = SessionManager(workspace)

    async def metadata(self, chat_id, metadata=None):
        from nanobot.session.display_history import HISTORY_GENERATION_METADATA_KEY

        session = await self.sessions.state.get_header(f"websocket:{chat_id}")
        return {**(metadata or {}), HISTORY_GENERATION_METADATA_KEY: session.generation}

    async def message(self, **kwargs):
        from nanobot.bus.events import OutboundMessage

        kwargs["metadata"] = await self.metadata(kwargs["chat_id"], kwargs.get("metadata"))
        return OutboundMessage(**kwargs)

    async def stream(self, channel, method, chat_id, *args, **kwargs):
        kwargs["metadata"] = await self.metadata(chat_id, kwargs.get("metadata"))
        await getattr(channel, method)(chat_id, *args, **kwargs)

    def append(self, key, record):
        from nanobot.webui.transcript import append_transcript_object

        append_transcript_object(key, record, sessions=self.sessions)

    def _project(self, name, key, *args, **kwargs):
        from nanobot.webui import transcript

        return self.sessions.project_history(key, lambda snapshot: getattr(transcript, name)(
            key, *args, history=snapshot, **kwargs,
        ))

    def read(self, key):
        return self._project("read_transcript_lines", key)

    def build(self, key, **kwargs):
        return self._project("build_webui_thread_response", key, **kwargs)

    def trace(self, key, detail_ref):
        return self._project("build_webui_trace_detail_response", key, detail_ref)

    def revision(self, key, **kwargs):
        return self._project("webui_transcript_revision", key, **kwargs)

    def prompts(self, key, **kwargs):
        return self._project("build_prompt_index", key, **kwargs)

    def prompt_response(self, key, previous_revision, **kwargs):
        return self._project("prompt_index_response", key, previous_revision, **kwargs)

    def unfinished(self, key):
        return self._project("has_unfinished_transcript_tail", key)


@pytest.fixture
async def history(tmp_path: Path):
    fixture = DisplayHistoryFixture(tmp_path)
    yield fixture
    if fixture.sessions._state is not None:
        await fixture.sessions.state.aclose()
