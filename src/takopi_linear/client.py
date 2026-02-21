from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import anyio
import httpx

from .types import (
    LinearAgentActivity,
    LinearAgentSession,
    LinearIssue,
    LinearUser,
    LinearWorkflowState,
    PlanStep,
)

DEFAULT_API_URL: Final[str] = "https://api.linear.app/graphql"


class LinearApiError(RuntimeError):
    pass


@dataclass(slots=True)
class _RateLimiter:
    max_requests: int
    window_s: float
    clock: Any = time.monotonic
    _lock: Any = field(init=False, repr=False)
    _events: deque[float] = field(init=False, repr=False, default_factory=deque)

    def __post_init__(self) -> None:
        self._lock = anyio.Lock()

    async def acquire(self) -> None:
        if self.max_requests <= 0:
            return
        async with self._lock:
            now = float(self.clock())
            cutoff = now - float(self.window_s)
            while self._events and self._events[0] <= cutoff:
                self._events.popleft()
            if len(self._events) < self.max_requests:
                self._events.append(now)
                return
            sleep_for = (self._events[0] + float(self.window_s)) - now
            if sleep_for > 0:
                await anyio.sleep(sleep_for)
            now = float(self.clock())
            cutoff = now - float(self.window_s)
            while self._events and self._events[0] <= cutoff:
                self._events.popleft()
            self._events.append(now)


class LinearClient:
    def __init__(
        self,
        oauth_token: str,
        *,
        api_url: str = DEFAULT_API_URL,
        http: httpx.AsyncClient | None = None,
        rate_limit_per_hour: int = 500,
    ) -> None:
        self._api_url = api_url
        self._token = oauth_token
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        if "Authorization" not in self._http.headers:
            self._http.headers["Authorization"] = f"Bearer {oauth_token}"
        if "Content-Type" not in self._http.headers:
            self._http.headers["Content-Type"] = "application/json"
        self._rate = _RateLimiter(max_requests=rate_limit_per_hour, window_s=3600.0)

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    async def graphql(
        self,
        query: str,
        *,
        variables: Mapping[str, Any] | None = None,
        operation_name: str | None = None,
    ) -> dict[str, Any]:
        await self._rate.acquire()
        payload: dict[str, Any] = {"query": query, "variables": dict(variables or {})}
        if operation_name is not None:
            payload["operationName"] = operation_name
        try:
            resp = await self._http.post(self._api_url, content=json.dumps(payload))
        except httpx.HTTPError as exc:
            raise LinearApiError(f"Linear request failed: {exc}") from exc
        if resp.status_code >= 400:
            body = resp.text.strip()
            raise LinearApiError(
                f"Linear API HTTP {resp.status_code}: {body or '<empty>'}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise LinearApiError(f"Linear API returned invalid JSON: {resp.text}") from exc
        if not isinstance(data, dict):
            raise LinearApiError(f"Linear API returned invalid payload: {data!r}")
        errors = data.get("errors")
        if errors:
            raise LinearApiError(f"Linear GraphQL error: {errors!r}")
        result = data.get("data")
        if not isinstance(result, dict):
            raise LinearApiError(f"Linear API returned no data: {data!r}")
        return result

    async def get_viewer(self) -> LinearUser:
        query = """
        query Me {
          viewer {
            id
            name
            email
          }
        }
        """
        data = await self.graphql(query, operation_name="Me")
        viewer = data.get("viewer")
        if not isinstance(viewer, dict):
            raise LinearApiError("Missing viewer in response")
        return viewer  # type: ignore[return-value]

    async def get_issue(self, issue_id: str) -> LinearIssue:
        query = """
        query Issue($id: String!) {
          issue(id: $id) {
            id
            title
            identifier
            url
            team { id key name }
            project { id name }
            state { id name type }
          }
        }
        """
        data = await self.graphql(query, variables={"id": issue_id}, operation_name="Issue")
        issue = data.get("issue")
        if not isinstance(issue, dict):
            raise LinearApiError("Missing issue in response")
        return issue  # type: ignore[return-value]

    async def update_issue(self, issue_id: str, **fields: Any) -> LinearIssue:
        query = """
        mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
          issueUpdate(id: $id, input: $input) {
            success
            issue {
              id
              title
              identifier
              url
              team { id key name }
              project { id name }
              state { id name type }
            }
          }
        }
        """
        data = await self.graphql(
            query,
            variables={"id": issue_id, "input": fields},
            operation_name="IssueUpdate",
        )
        payload = data.get("issueUpdate")
        if not isinstance(payload, dict) or not payload.get("success"):
            raise LinearApiError(f"Issue update failed: {payload!r}")
        issue = payload.get("issue")
        if not isinstance(issue, dict):
            raise LinearApiError("Missing updated issue in response")
        return issue  # type: ignore[return-value]

    async def get_workflow_states(self, team_id: str) -> list[LinearWorkflowState]:
        query = """
        query WorkflowStates($teamId: ID!) {
          workflowStates(filter: { team: { id: { eq: $teamId } } }) {
            nodes {
              id
              name
              type
            }
          }
        }
        """
        data = await self.graphql(
            query,
            variables={"teamId": team_id},
            operation_name="WorkflowStates",
        )
        root = data.get("workflowStates")
        if not isinstance(root, dict):
            return []
        nodes = root.get("nodes")
        if not isinstance(nodes, list):
            return []
        return [node for node in nodes if isinstance(node, dict)]  # type: ignore[return-value]

    async def create_agent_activity(
        self,
        *,
        session_id: str,
        content: Mapping[str, Any],
        ephemeral: bool | None = None,
    ) -> LinearAgentActivity:
        query = """
        mutation AgentActivityCreate($input: AgentActivityCreateInput!) {
          agentActivityCreate(input: $input) {
            success
            agentActivity {
              id
            }
          }
        }
        """
        input_payload: dict[str, Any] = {
            "agentSessionId": session_id,
            "content": dict(content),
        }
        if ephemeral is not None:
            input_payload["ephemeral"] = bool(ephemeral)
        data = await self.graphql(
            query,
            variables={"input": input_payload},
            operation_name="AgentActivityCreate",
        )
        payload = data.get("agentActivityCreate")
        if not isinstance(payload, dict) or not payload.get("success"):
            raise LinearApiError(f"Agent activity create failed: {payload!r}")
        activity = payload.get("agentActivity")
        if not isinstance(activity, dict):
            raise LinearApiError("Missing agentActivity in response")
        return activity  # type: ignore[return-value]

    async def update_agent_session(
        self,
        *,
        session_id: str,
        data: Mapping[str, Any],
    ) -> LinearAgentSession:
        query = """
        mutation AgentSessionUpdate($agentSessionId: String!, $data: AgentSessionUpdateInput!) {
          agentSessionUpdate(id: $agentSessionId, input: $data) {
            success
            agentSession {
              id
              state
            }
          }
        }
        """
        result = await self.graphql(
            query,
            variables={"agentSessionId": session_id, "data": dict(data)},
            operation_name="AgentSessionUpdate",
        )
        payload = result.get("agentSessionUpdate")
        if not isinstance(payload, dict) or not payload.get("success"):
            raise LinearApiError(f"Agent session update failed: {payload!r}")
        session = payload.get("agentSession")
        if not isinstance(session, dict):
            return {"id": session_id}  # type: ignore[return-value]
        return session  # type: ignore[return-value]

    async def set_agent_plan(self, *, session_id: str, steps: list[PlanStep]) -> None:
        await self.update_agent_session(session_id=session_id, data={"plan": steps})
