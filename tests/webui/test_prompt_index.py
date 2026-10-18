"""Complete, lightweight navigation uses the same IDs and cursors as replay."""

import json
import time

import pytest
from websockets.datastructures import Headers
from websockets.http11 import Request

from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.runtime import WebSocketConfig
from nanobot.webui import transcript
from nanobot.webui.client_contract import webui_contract
from nanobot.webui.gateway_services import build_gateway_services


def test_complete_index_and_bidirectional_pages_without_loading_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(transcript, "_ACTIVE_TRANSCRIPT_ROTATE_BYTES", 2000)
    monkeypatch.setattr(transcript, "_TARGET_ACTIVE_TRANSCRIPT_BYTES", 1000)
    key = "websocket:navigation"
    for i in range(120):
        transcript.append_transcript_object(key, {"event": "user", "chat_id": "navigation", "text": f"question {i}", "turn_id": f"t{i}"})
        transcript.append_transcript_object(key, {"event": "message", "chat_id": "navigation", "text": "answer " + "x" * 200, "turn_id": f"t{i}"})
        transcript.append_transcript_object(key, {"event": "turn_end", "chat_id": "navigation", "turn_id": f"t{i}"})
    index = transcript.build_prompt_index(key)
    assert len(index) == 120
    assert not (tmp_path / "sessions" / "sessions.sqlite3").exists()
    latest = transcript.build_webui_thread_response(key, limit=3)
    assert len(latest["events"]) < len(index)
    target = index[42]
    stats = transcript.TranscriptReplayStats()
    page = transcript.build_webui_thread_response(key, after=target["cursor"], limit=6, stats=stats)
    assert page["events"][0]["projection_id"] == target["id"]
    assert page["events"][0]["text"] == "question 42"
    assert page["page"]["user_message_offset"] == 42
    assert page["page"]["has_more_before"] and page["page"]["has_more_after"]
    assert stats.parsed_records < 40  # Not the 360-record transcript.
    newer = transcript.build_webui_thread_response(key, after=page["page"]["after_cursor"], limit=6)
    older = transcript.build_webui_thread_response(key, before=newer["page"]["before_cursor"], limit=6)
    assert older["events"] == page["events"]
    # Warm navigation uses file metadata + cached summaries, not replay.
    with monkeypatch.context() as patch:
        patch.setattr(transcript, "_read_transcript_file", lambda _: (_ for _ in ()).throw(AssertionError("replayed history")))
        assert transcript.build_prompt_index(key) == index
    transcript.append_transcript_object(key, {"event": "user", "chat_id": "navigation", "text": "new question"})
    assert len(transcript.build_prompt_index(key)) == 121


def test_oversized_turn_keeps_matching_navigation_identity(tmp_path, monkeypatch):
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(transcript, "_MAX_TRANSCRIPT_PAGE_RECORDS", 6)
    key = "websocket:large-turn"
    transcript.append_transcript_object(key, {"event": "user", "chat_id": "large-turn", "text": "target"})
    for i in range(20):
        transcript.append_transcript_object(key, {"event": "message", "chat_id": "large-turn", "kind": "tool_hint", "text": str(i)})
    transcript.append_transcript_object(key, {"event": "message", "chat_id": "large-turn", "text": "done"})
    transcript.append_transcript_object(key, {"event": "turn_end", "chat_id": "large-turn"})
    target = transcript.build_prompt_index(key)[0]
    page = transcript.build_webui_thread_response(key, after=target["cursor"], limit=40)
    assert page["page"]["truncated_oversized_turn"]
    assert page["events"][0]["projection_id"] == target["id"]
    assert len(target["answerPreview"]) <= 240


@pytest.mark.asyncio
async def test_navigation_public_routes_auth_validation_and_old_reads(tmp_path, monkeypatch):
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)
    gateway = build_gateway_services(
        config=WebSocketConfig(), bus=MessageBus(), session_manager=None,
        static_dist_path=None, workspace_path=tmp_path, config_path=tmp_path / "config.json",
        default_restrict_to_workspace=False, runtime_model_name=None,
        runtime_surface="browser", runtime_capabilities_overrides=None,
    )
    path = "/api/sessions/websocket%3Atest/webui-thread"
    denied = await gateway.http._dispatch_session_routes(Request(path + "/prompts", Headers()), path + "/prompts")
    assert denied.status_code == 401
    gateway.tokens.api_tokens["test-token"] = time.monotonic() + 60
    headers = Headers([("Authorization", "Bearer test-token")])
    transcript.append_transcript_object("websocket:test", {"event": "user", "chat_id": "test", "text": "hello"})
    response = await gateway.http._dispatch_session_routes(Request(path + "/prompts", headers), path + "/prompts")
    prompt = json.loads(response.body)["prompts"][0]
    current = await gateway.http._dispatch_session_routes(Request(path + "?after=" + prompt["cursor"], headers), path)
    legacy = await gateway.http._dispatch_session_routes(Request(path + "?direction=latest&limit=40", headers), path)
    assert current.status_code == legacy.status_code == 200
    assert json.loads(current.body)["events"] == json.loads(legacy.body)["events"]
    invalid = await gateway.http._dispatch_session_routes(Request(path + "?after=bad", headers), path)
    assert invalid.status_code == 400
    assert "webui.prompt-navigation.v1" in webui_contract()["capabilities"]
