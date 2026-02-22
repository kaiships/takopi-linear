from __future__ import annotations

from takopi_linear.backend import (
    _extract_issue_project_id,
    _extract_issue_title,
    _extract_issue_title_from_prompt_context,
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


def test_extracts_prompted_body_from_agent_activity_content_body() -> None:
    payload = {
        "type": "AgentSessionEvent",
        "action": "prompted",
        "data": {
            "agentSession": {"id": "sess_1"},
            "agentActivity": {"content": {"type": "message", "body": "hello from content"}},
        },
    }
    raw = _unwrap_payload(payload)
    assert _extract_session_id(raw) == "sess_1"
    assert _extract_prompt_body(raw) == "hello from content"


def test_extracts_prompted_body_from_agent_activity_content_nested_message_body() -> None:
    payload = {
        "type": "AgentSessionEvent",
        "action": "prompted",
        "agentSession": {"id": "sess_1"},
        "agentActivity": {"content": {"type": "message", "message": {"body": "hello nested"}}},
    }
    raw = _unwrap_payload(payload)
    assert _extract_prompt_body(raw) == "hello nested"


def test_extracts_prompted_body_from_agent_activity_content_json_string() -> None:
    payload = {
        "type": "AgentSessionEvent",
        "action": "prompted",
        "agentSession": {"id": "sess_1"},
        "agentActivity": {"content": '{"type":"message","message":{"body":"hello json"}}'},
    }
    raw = _unwrap_payload(payload)
    assert _extract_prompt_body(raw) == "hello json"


def test_extracts_prompted_body_from_agent_activity_content_action_message_parameter() -> None:
    payload = {
        "type": "AgentSessionEvent",
        "action": "prompted",
        "agentSession": {"id": "sess_1"},
        "agentActivity": {"content": {"type": "action", "action": "message", "parameter": "hello param"}},
    }
    raw = _unwrap_payload(payload)
    assert _extract_prompt_body(raw) == "hello param"


def test_extracts_prompted_body_from_agent_activity_content_nested_action_message_parameter() -> None:
    payload = {
        "type": "AgentSessionEvent",
        "action": "prompted",
        "agentSession": {"id": "sess_1"},
        "agentActivity": {
            "content": {"type": "action", "action": {"action": "message", "parameter": "hello nested param"}}
        },
    }
    raw = _unwrap_payload(payload)
    assert _extract_prompt_body(raw) == "hello nested param"


def test_extracts_issue_title_from_prompt_context_title_tag() -> None:
    payload = {"promptContext": "<issue><title>@fix/test Extract title</title></issue>"}
    raw = _unwrap_payload(payload)
    assert _extract_issue_title(raw) is None
    assert _extract_issue_title_from_prompt_context(raw) == "@fix/test Extract title"


def test_extracts_fields_with_snake_case_keys() -> None:
    payload = {
        "agent_session": {
            "id": "sess_1",
            "issue": {"title": "Snake case title", "project_id": "proj_1"},
        }
    }
    raw = _unwrap_payload(payload)
    assert _extract_session_id(raw) == "sess_1"
    assert _extract_issue_title(raw) == "Snake case title"
    assert _extract_issue_project_id(raw) == "proj_1"
