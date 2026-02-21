from __future__ import annotations

from takopi.transport import RenderedMessage

from takopi_linear.bridge import _activity_from_message


def test_renders_thought_activity_content() -> None:
    spec = _activity_from_message(
        RenderedMessage(text="hello", extra={"activity_type": "thought", "ephemeral": True}),
        default_type="thought",
    )
    assert spec.content == {"type": "thought", "body": "hello"}
    assert spec.ephemeral is True


def test_renders_response_activity_content() -> None:
    spec = _activity_from_message(
        RenderedMessage(text="done", extra={"activity_type": "response"}),
        default_type="thought",
    )
    assert spec.content == {"type": "response", "body": "done"}
    assert spec.ephemeral is None


def test_renders_action_activity_content() -> None:
    spec = _activity_from_message(
        RenderedMessage(
            text="fallback",
            extra={
                "activity_type": "action",
                "action": "run_tests",
                "parameter": "pytest -q",
                "result": "ok",
                "ephemeral": True,
            },
        ),
        default_type="thought",
    )
    assert spec.content == {
        "type": "action",
        "action": "run_tests",
        "parameter": "pytest -q",
        "result": "ok",
    }
    assert spec.ephemeral is True


def test_renders_action_activity_content_fallback() -> None:
    spec = _activity_from_message(
        RenderedMessage(text="ping", extra={"activity_type": "action", "ephemeral": True}),
        default_type="thought",
    )
    assert spec.content == {"type": "action", "action": "message", "parameter": "ping"}
    assert spec.ephemeral is True

