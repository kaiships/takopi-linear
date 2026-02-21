from __future__ import annotations

from takopi_linear.backend import (
    _extract_issue_project_id,
    _extract_issue_title,
    _extract_prompt_body,
    _extract_session_id,
    _normalize_event_type,
    _unwrap_payload,
)

def test_normalizes_agent_session_event_types() -> None:
    assert _normalize_event_type("AgentSessionEvent", "created") == "agent_session.created"
    assert _normalize_event_type("agentsessionevent.created", None) == "agent_session.created"
    assert _normalize_event_type("agentsessionevent.prompted", None) == "agent_session.prompted"
    assert _normalize_event_type("AgentSessionEvent", "stopped") == "agent_session.stopped"
    assert _normalize_event_type("agentsessionevent.stopped", None) == "agent_session.stopped"
    assert _normalize_event_type("AgentSessionEvent", "cancelled") == "agent_session.cancelled"
    assert _normalize_event_type("AgentSessionEvent", "canceled") == "agent_session.canceled"


def test_extracts_agent_session_created_fields() -> None:
    payload = {
        "type": "AgentSessionEvent",
        "action": "created",
        "agentSession": {
            "id": "sess_1",
            "issue": {"title": "@feat/auth Add JWT", "project": {"id": "proj_1"}},
        },
        "promptContext": "ignored",
    }
    raw = _unwrap_payload(payload)
    assert _extract_session_id(raw) == "sess_1"
    assert _extract_issue_title(raw) == "@feat/auth Add JWT"
    assert _extract_issue_project_id(raw) == "proj_1"


def test_extracts_prompted_body_from_agent_activity() -> None:
    payload = {
        "type": "AgentSessionEvent",
        "action": "prompted",
        "agentSession": {"id": "sess_1"},
        "agentActivity": {"body": "please continue"},
    }
    raw = _unwrap_payload(payload)
    assert _extract_session_id(raw) == "sess_1"
    assert _extract_prompt_body(raw) == "please continue"
