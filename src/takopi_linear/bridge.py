from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from takopi.markdown import MarkdownFormatter, assemble_markdown_parts
from takopi.progress import ProgressState
from takopi.transport import MessageRef, RenderedMessage, SendOptions

from .client import LinearClient
from .types import AgentActivityType


def _split_text(text: str, *, max_chars: int) -> list[str]:
    text = (text or "").strip()
    if not text:
        return [""]
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    start = 0
    n = len(text)
    min_chunk = max(1, int(max_chars * 0.5))
    while start < n:
        end = min(start + max_chars, n)
        if end == n:
            chunk = text[start:end].rstrip()
            if chunk:
                parts.append(chunk)
            break
        cut = text.rfind("\n\n", start, end)
        if cut < start + min_chunk:
            cut = text.rfind("\n", start, end)
        if cut <= start:
            cut = end
        chunk = text[start:cut].rstrip()
        if chunk:
            parts.append(chunk)
        start = cut
        while start < n and text[start] == "\n":
            start += 1
    return parts or [text[:max_chars].rstrip()]


class LinearPresenter:
    def __init__(
        self,
        *,
        formatter: MarkdownFormatter | None = None,
        message_overflow: str = "split",
        max_body_chars: int = 10_000,
    ) -> None:
        self._formatter = formatter or MarkdownFormatter()
        self._message_overflow = message_overflow
        self._max_body_chars = int(max_body_chars)

    def render_progress(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        label: str = "working",
    ) -> RenderedMessage:
        parts = self._formatter.render_progress_parts(state, elapsed_s=elapsed_s, label=label)
        text = assemble_markdown_parts(parts)
        return RenderedMessage(
            text=text,
            extra={
                "activity_type": "thought",
                "ephemeral": True,
            },
        )

    def render_final(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        status: str,
        answer: str,
    ) -> RenderedMessage:
        parts = self._formatter.render_final_parts(
            state, elapsed_s=elapsed_s, status=status, answer=answer
        )
        text = assemble_markdown_parts(parts)

        activity_type: AgentActivityType = "response"
        if status == "error":
            activity_type = "error"

        if self._message_overflow == "trim":
            if len(text) > self._max_body_chars:
                text = text[: max(0, self._max_body_chars - 1)].rstrip() + "…"
            return RenderedMessage(text=text, extra={"activity_type": activity_type})

        chunks = _split_text(text, max_chars=self._max_body_chars)
        if len(chunks) <= 1:
            return RenderedMessage(text=text, extra={"activity_type": activity_type})

        followups = [
            RenderedMessage(text=chunk, extra={"activity_type": activity_type})
            for chunk in chunks[1:]
        ]
        return RenderedMessage(
            text=chunks[0],
            extra={"activity_type": activity_type, "followups": followups},
        )


@dataclass(slots=True)
class _ActivitySpec:
    type: AgentActivityType
    content: dict[str, Any]
    ephemeral: bool | None


def _activity_from_message(message: RenderedMessage, *, default_type: AgentActivityType) -> _ActivitySpec:
    raw_type = message.extra.get("activity_type", default_type)
    activity_type: AgentActivityType
    if raw_type in {"thought", "action", "elicitation", "response", "error"}:
        activity_type = cast(AgentActivityType, raw_type)
    else:
        activity_type = default_type

    ephemeral: bool | None = None
    if "ephemeral" in message.extra:
        ephemeral = bool(message.extra.get("ephemeral"))
    if activity_type not in {"thought", "action"}:
        ephemeral = None

    if activity_type == "action":
        action = message.extra.get("action")
        parameter = message.extra.get("parameter")
        result = message.extra.get("result")
        content: dict[str, Any] = {"type": "action", "action": {}}
        if isinstance(action, str):
            content["action"]["action"] = action
        if isinstance(parameter, str):
            content["action"]["parameter"] = parameter
        if isinstance(result, str):
            content["action"]["result"] = result
        if not content["action"]:
            content["action"] = {"action": "message", "parameter": message.text}
        return _ActivitySpec(type=activity_type, content=content, ephemeral=ephemeral)

    content = {
        "type": activity_type,
        activity_type: {
            "body": message.text,
        },
    }
    return _ActivitySpec(type=activity_type, content=content, ephemeral=ephemeral)


class LinearTransport:
    def __init__(self, client: LinearClient) -> None:
        self._client = client

    @staticmethod
    def _extract_followups(message: RenderedMessage) -> list[RenderedMessage]:
        followups = message.extra.get("followups")
        if not isinstance(followups, list):
            return []
        return [item for item in followups if isinstance(item, RenderedMessage)]

    async def close(self) -> None:
        await self._client.aclose()

    async def send(
        self,
        *,
        channel_id: int | str,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef | None:
        session_id = str(channel_id)
        followups = self._extract_followups(message)
        _ = options  # no per-message reply/edit semantics

        spec = _activity_from_message(message, default_type="thought")
        activity = await self._client.create_agent_activity(
            session_id=session_id,
            content=spec.content,
            ephemeral=spec.ephemeral,
        )
        ref = MessageRef(
            channel_id=session_id,
            message_id=str(activity.get("id") or ""),
            raw=activity,
            thread_id=None,
        )
        for followup in followups:
            spec2 = _activity_from_message(followup, default_type=spec.type)
            await self._client.create_agent_activity(
                session_id=session_id,
                content=spec2.content,
                ephemeral=spec2.ephemeral,
            )
        return ref

    async def edit(
        self,
        *,
        ref: MessageRef,
        message: RenderedMessage,
        wait: bool = True,
    ) -> MessageRef | None:
        # Linear's Agent Activities support ephemeral updates; prefer emitting a new activity.
        # (Ephemeral activities are replaced by subsequent activities in the UI.)
        session_id = str(ref.channel_id)
        _ = wait
        followups = self._extract_followups(message)
        spec = _activity_from_message(message, default_type="thought")
        activity = await self._client.create_agent_activity(
            session_id=session_id,
            content=spec.content,
            ephemeral=spec.ephemeral,
        )
        out = MessageRef(
            channel_id=session_id,
            message_id=str(activity.get("id") or ref.message_id),
            raw=activity,
            thread_id=None,
        )
        for followup in followups:
            spec2 = _activity_from_message(followup, default_type=spec.type)
            await self._client.create_agent_activity(
                session_id=session_id,
                content=spec2.content,
                ephemeral=spec2.ephemeral,
            )
        return out

    async def delete(self, *, ref: MessageRef) -> bool:
        # No delete API is required for the intended ephemeral-based UX.
        return True
