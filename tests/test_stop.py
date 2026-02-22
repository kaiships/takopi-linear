from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from takopi.runner_bridge import ExecBridgeConfig, RunningTask
from takopi.transport import MessageRef, RenderedMessage

from takopi_linear.backend import _SessionState, _handle_event
from takopi_linear.settings import LinearTransportSettings
from takopi_linear.types import GatewayEvent


class _FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, RenderedMessage]] = []

    async def close(self) -> None:
        return None

    async def send(self, *, channel_id: str, message: RenderedMessage, options: Any | None = None):
        _ = options
        self.sent.append((str(channel_id), message))
        return None

    async def edit(self, *, ref: MessageRef, message: RenderedMessage, wait: bool = True):
        _ = ref
        _ = message
        _ = wait
        return None

    async def delete(self, *, ref: MessageRef) -> bool:
        _ = ref
        return True


class _FakePresenter:
    pass


@pytest.mark.anyio
async def test_stop_event_requests_cancel_for_running_session() -> None:
    transport = _FakeTransport()
    exec_cfg = ExecBridgeConfig(transport=transport, presenter=_FakePresenter(), final_notify=False)
    settings = LinearTransportSettings(
        oauth_token="token",
        app_id="app",
        gateway_database_url="postgresql://example",
    )

    state = _SessionState()
    running = RunningTask()
    state.running_tasks[MessageRef(channel_id="sess_1", message_id="m1")] = running

    event = GatewayEvent(
        id="e2",
        source="linear",
        event_type="AgentSessionEvent",
        payload={
            "type": "AgentSessionEvent",
            "action": "stopped",
            "agentSession": {"id": "sess_1"},
        },
        external_id=None,
        created_at=None,
    )

    await _handle_event(
        event=event,
        runtime=cast(Any, object()),
        exec_cfg=exec_cfg,
        client=cast(Any, object()),
        settings=settings,
        project_map={},
        sessions={"sess_1": state},
        default_engine_override=None,
    )

    assert state.stop_requested is True
    assert running.cancel_requested.is_set() is True
    assert transport.sent
    assert transport.sent[-1][0] == "sess_1"
    assert "Stop requested" in transport.sent[-1][1].text


class _FakeRunner:
    engine = "fake"

    def is_resume_line(self, line: str) -> bool:
        _ = line
        return False

    def extract_resume(self, text: str | None):
        _ = text
        return None

    def run(self, prompt: str, resume):
        _ = (prompt, resume)
        return None


class _FakeRuntime:
    def __init__(self) -> None:
        self.seen_text: str | None = None

    def resolve_message(self, *, text: str, reply_text, ambient_context, chat_id):
        _ = (reply_text, ambient_context, chat_id)
        self.seen_text = text
        return cast(
            Any,
            type(
                "_Resolved",
                (),
                {"prompt": text, "engine_override": None, "resume_token": None, "context": None},
            )(),
        )

    def resolve_runner(self, *, resume_token, engine_override):
        _ = (resume_token, engine_override)
        return cast(Any, type("_Entry", (), {"runner": _FakeRunner(), "available": True, "issue": None})())

    def resolve_run_cwd(self, context):
        _ = context
        return Path("/tmp")

    def format_context_line(self, context):
        _ = context
        return ""

    def is_resume_line(self, line: str) -> bool:
        _ = line
        return False


class _FakeClient:
    def __init__(self) -> None:
        self.seen_activity_id: str | None = None

    async def get_agent_activity(self, activity_id: str):
        self.seen_activity_id = activity_id
        return {"id": activity_id, "content": {"__typename": "AgentActivityPromptContent", "body": "fetched prompt"}}


@pytest.mark.anyio
async def test_prompted_event_fetches_prompt_body_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_handle_message(*args, **kwargs):
        _ = (args, kwargs)
        return None

    monkeypatch.setattr("takopi_linear.backend.handle_message", fake_handle_message)

    transport = _FakeTransport()
    exec_cfg = ExecBridgeConfig(transport=transport, presenter=_FakePresenter(), final_notify=False)
    settings = LinearTransportSettings(
        oauth_token="token",
        app_id="app",
        gateway_database_url="postgresql://example",
    )
    client = _FakeClient()
    runtime = _FakeRuntime()

    event = GatewayEvent(
        id="e1",
        source="linear",
        event_type="AgentSessionEvent",
        payload={
            "type": "AgentSessionEvent",
            "action": "prompted",
            "agentSession": {"id": "sess_1"},
            "agentActivity": {"id": "act_1"},
        },
        external_id=None,
        created_at=None,
    )

    await _handle_event(
        event=event,
        runtime=cast(Any, runtime),
        exec_cfg=exec_cfg,
        client=cast(Any, client),
        settings=settings,
        project_map={},
        sessions={},
        default_engine_override=None,
    )

    assert client.seen_activity_id == "act_1"
    assert runtime.seen_text == "fetched prompt"
    assert any("Acknowledged" in msg.text for _, msg in transport.sent)


class _FakeSnapshotClient:
    def __init__(self) -> None:
        self.seen_session_id: str | None = None

    async def set_agent_plan(self, *, session_id: str, steps):
        _ = (session_id, steps)
        return None

    async def get_agent_session_snapshot(self, session_id: str, *, activities_last: int = 10):
        self.seen_session_id = session_id
        _ = activities_last
        return {
            "id": session_id,
            "issue": {"id": "iss_1", "title": "Snapshot issue", "project": {"id": "proj_1"}},
            "activities": {
                "nodes": [
                    {
                        "id": "act_1",
                        "createdAt": "2026-02-22T15:46:48.125Z",
                        "content": {"__typename": "AgentActivityPromptContent", "body": "snapshot prompt"},
                    }
                ]
            },
        }


@pytest.mark.anyio
async def test_prompted_event_fetches_prompt_from_session_snapshot_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_handle_message(*args, **kwargs):
        _ = (args, kwargs)
        return None

    monkeypatch.setattr("takopi_linear.backend.handle_message", fake_handle_message)

    transport = _FakeTransport()
    exec_cfg = ExecBridgeConfig(transport=transport, presenter=_FakePresenter(), final_notify=False)
    settings = LinearTransportSettings(
        oauth_token="token",
        app_id="app",
        gateway_database_url="postgresql://example",
    )
    client = _FakeSnapshotClient()
    runtime = _FakeRuntime()

    event = GatewayEvent(
        id="e1",
        source="linear",
        event_type="agentsessionevent.prompted",
        payload={
            "type": "agentsessionevent.prompted",
            "data": {"agentSessionId": "sess_1"},
        },
        external_id=None,
        created_at=None,
    )

    await _handle_event(
        event=event,
        runtime=cast(Any, runtime),
        exec_cfg=exec_cfg,
        client=cast(Any, client),
        settings=settings,
        project_map={"proj_1": "takopi-linear"},
        sessions={},
        default_engine_override=None,
    )

    assert client.seen_session_id == "sess_1"
    assert runtime.seen_text == "snapshot prompt"


@pytest.mark.anyio
async def test_created_event_builds_prompt_from_issue_title_and_snapshot_prompt_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_handle_message(*args, **kwargs):
        _ = (args, kwargs)
        return None

    monkeypatch.setattr("takopi_linear.backend.handle_message", fake_handle_message)

    transport = _FakeTransport()
    exec_cfg = ExecBridgeConfig(transport=transport, presenter=_FakePresenter(), final_notify=False)
    settings = LinearTransportSettings(
        oauth_token="token",
        app_id="app",
        gateway_database_url="postgresql://example",
    )
    client = _FakeSnapshotClient()
    runtime = _FakeRuntime()

    event = GatewayEvent(
        id="e1",
        source="linear",
        event_type="agentsessionevent.created",
        payload={
            "type": "agentsessionevent.created",
            "data": {"agentSessionId": "sess_1"},
        },
        external_id=None,
        created_at=None,
    )

    await _handle_event(
        event=event,
        runtime=cast(Any, runtime),
        exec_cfg=exec_cfg,
        client=cast(Any, client),
        settings=settings,
        project_map={"proj_1": "takopi-linear"},
        sessions={},
        default_engine_override=None,
    )

    assert client.seen_session_id == "sess_1"
    assert runtime.seen_text == "Snapshot issue\n\nsnapshot prompt"
