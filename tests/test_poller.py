from __future__ import annotations

import pytest

from takopi_linear.poller import GatewayPoller


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, sql: str, params: tuple[object, ...]):
        self.executed.append((sql, params))

    async def fetchall(self):
        return self.rows


class _FakeConn:
    def __init__(self, rows):
        self._cursor = _FakeCursor(rows)
        self.commits = 0

    def cursor(self):
        return self._cursor

    async def commit(self):
        self.commits += 1

    async def close(self):
        return None


@pytest.mark.anyio
async def test_poller_claims_and_commits() -> None:
    conn = _FakeConn(
        [
            {
                "id": "e1",
                "source": "linear",
                "event_type": "agent_session.created",
                "external_id": None,
                "payload": {"x": 1},
                "created_at": None,
            }
        ]
    )
    poller = GatewayPoller(
        database_url="postgresql://example",
        conn=conn,
        source="linear",
        batch_size=10,
    )
    events = await poller.poll()
    assert len(events) == 1
    assert events[0].id == "e1"
    assert conn.commits == 1


@pytest.mark.anyio
async def test_poller_decodes_json_payload_strings() -> None:
    conn = _FakeConn(
        [
            {
                "id": "e1",
                "source": "linear",
                "event_type": "agent_session.prompted",
                "external_id": None,
                "payload": '{"agentSessionId": "sess_1", "agentActivity": {"body": "hi"}}',
                "created_at": None,
            }
        ]
    )
    poller = GatewayPoller(database_url="postgresql://example", conn=conn)
    events = await poller.poll()
    assert events[0].payload["agentSessionId"] == "sess_1"
    assert events[0].payload["agentActivity"]["body"] == "hi"


@pytest.mark.anyio
async def test_poller_marks_done_and_failed() -> None:
    conn = _FakeConn([])
    poller = GatewayPoller(database_url="postgresql://example", conn=conn)
    await poller.mark_done("e1")
    await poller.mark_failed("e2", error="boom")
    assert conn.commits == 2
