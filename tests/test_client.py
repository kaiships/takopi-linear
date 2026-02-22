from __future__ import annotations

import json

import httpx
import pytest

from takopi_linear.client import LinearClient


@pytest.mark.anyio
async def test_get_viewer_sends_auth_header() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer test-token"
        body = json.loads(request.content.decode("utf-8"))
        assert "query" in body
        assert body.get("operationName") == "Me"
        return httpx.Response(
            200,
            json={"data": {"viewer": {"id": "u1", "name": "Kai", "email": "k@x"}}},
        )

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = LinearClient("test-token", api_url="https://linear.test/graphql", http=http)
    try:
        viewer = await client.get_viewer()
        assert viewer["id"] == "u1"
        assert viewer["name"] == "Kai"
    finally:
        await http.aclose()


@pytest.mark.anyio
async def test_create_agent_activity_includes_content_and_ephemeral() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen.update(body)
        return httpx.Response(
            200,
            json={
                "data": {
                    "agentActivityCreate": {
                        "success": True,
                        "agentActivity": {"id": "a1", "type": "thought", "body": "x"},
                    }
                }
            },
        )

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = LinearClient("t", api_url="https://linear.test/graphql", http=http)
    try:
        activity = await client.create_agent_activity(
            session_id="s1",
            content={"type": "thought", "thought": {"body": "hi"}},
            ephemeral=True,
        )
        assert activity["id"] == "a1"
        variables = seen.get("variables")
        assert isinstance(variables, dict)
        inp = variables.get("input")
        assert isinstance(inp, dict)
        assert inp["agentSessionId"] == "s1"
        assert inp["ephemeral"] is True
        assert inp["content"]["type"] == "thought"
    finally:
        await http.aclose()


@pytest.mark.anyio
async def test_get_agent_activity_fetches_content_body() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen.update(body)
        return httpx.Response(
            200,
            json={
                "data": {
                    "agentActivity": {
                        "id": "a1",
                        "content": {"__typename": "AgentActivityPromptContent", "body": "hello"},
                    }
                }
            },
        )

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = LinearClient("t", api_url="https://linear.test/graphql", http=http)
    try:
        activity = await client.get_agent_activity("a1")
        assert activity["id"] == "a1"
        assert activity["content"]["body"] == "hello"
        assert seen.get("operationName") == "AgentActivity"
        variables = seen.get("variables")
        assert isinstance(variables, dict)
        assert variables["id"] == "a1"
    finally:
        await http.aclose()


@pytest.mark.anyio
async def test_set_agent_plan_uses_agent_session_update() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body.get("operationName") == "AgentSessionUpdate"
        variables = body.get("variables")
        assert isinstance(variables, dict)
        assert variables["agentSessionId"] == "s1"
        assert variables["data"]["plan"][0]["content"] == "Analyze request"
        return httpx.Response(
            200,
            json={
                "data": {
                    "agentSessionUpdate": {
                        "success": True,
                        "agentSession": {"id": "s1", "state": "active"},
                    }
                }
            },
        )

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = LinearClient("t", api_url="https://linear.test/graphql", http=http)
    try:
        await client.set_agent_plan(
            session_id="s1",
            steps=[{"content": "Analyze request", "status": "inProgress"}],
        )
    finally:
        await http.aclose()
