from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypedDict

AgentActivityType = Literal["thought", "action", "elicitation", "response", "error"]
PlanStepStatus = Literal["pending", "inProgress", "completed", "canceled"]


class PlanStep(TypedDict):
    content: str
    status: PlanStepStatus


@dataclass(frozen=True, slots=True)
class GatewayEvent:
    id: str
    source: str
    event_type: str
    payload: dict[str, Any]
    external_id: str | None = None
    created_at: datetime | None = None


class LinearUser(TypedDict, total=False):
    id: str
    name: str
    email: str


class LinearIssue(TypedDict, total=False):
    id: str
    title: str
    identifier: str
    url: str
    team: dict[str, Any]
    project: dict[str, Any] | None
    state: dict[str, Any] | None


class LinearWorkflowState(TypedDict, total=False):
    id: str
    name: str
    type: str


class LinearAgentActivity(TypedDict, total=False):
    id: str
    type: str
    body: str
    createdAt: str


class LinearAgentSession(TypedDict, total=False):
    id: str
    state: str
    issue: LinearIssue | None

