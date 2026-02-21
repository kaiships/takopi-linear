from __future__ import annotations

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
