from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import anyio

from takopi.backends import EngineBackend, SetupIssue
from takopi.backends_helpers import install_issue
from takopi.config import ConfigError, read_config
from takopi.context import RunContext
from takopi.logging import clear_context, get_logger, bind_run_context
from takopi.model import ResumeToken
from takopi.router import RunnerUnavailableError
from takopi.runner import Runner
from takopi.runner_bridge import ExecBridgeConfig, IncomingMessage, RunningTasks, handle_message
from takopi.transport import RenderedMessage
from takopi.transport_runtime import TransportRuntime
from takopi.transports import SetupResult, TransportBackend
from takopi.utils.paths import reset_run_base_dir, set_run_base_dir

from .bridge import LinearPresenter, LinearTransport
from .client import LinearApiError, LinearClient
from .poller import GatewayPoller
from .settings import LinearTransportSettings
from .types import GatewayEvent, PlanStep

logger = get_logger(__name__)

_STOP_SESSION_EVENTS: set[str] = {
    "agent_session.canceled",
    "agent_session.cancelled",
    "agent_session.stopped",
}


def _expect_settings(transport_config: object) -> LinearTransportSettings:
    if isinstance(transport_config, LinearTransportSettings):
        return transport_config
    if isinstance(transport_config, dict):
        return LinearTransportSettings.model_validate(transport_config)
    raise TypeError("transport_config must be a dict or LinearTransportSettings")


def _display_path(path: Path) -> str:
    home = Path.home()
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


def _config_issue(path: Path, *, title: str) -> SetupIssue:
    return SetupIssue(title, (f"   {_display_path(path)}",))


def _resolve_takopi_config_path() -> Path:
    import takopi.config as takopi_config

    resolver = getattr(takopi_config, "resolve_config_path", None)
    if callable(resolver):
        return cast(Path, resolver())
    fallback = getattr(takopi_config, "HOME_CONFIG_PATH", None)
    if isinstance(fallback, Path):
        return fallback
    return Path.home() / ".takopi" / "takopi.toml"


def _load_linear_project_map(config_path: Path) -> dict[str, str]:
    """Return a mapping of Linear project id -> takopi project key (lowercase)."""
    try:
        raw = read_config(config_path)
    except ConfigError:
        return {}
    mapping: dict[str, str] = {}

    projects = raw.get("projects")
    if isinstance(projects, dict):
        for alias, cfg in projects.items():
            if not isinstance(alias, str) or not isinstance(cfg, dict):
                continue
            pid = cfg.get("linear_project_id")
            if isinstance(pid, str) and pid.strip():
                mapping[pid.strip()] = alias.strip().lower()

    plugins = raw.get("plugins")
    if isinstance(plugins, dict):
        linear = plugins.get("linear")
        if isinstance(linear, dict):
            project_map = linear.get("project_map")
            if isinstance(project_map, dict):
                for pid, alias in project_map.items():
                    if not isinstance(pid, str) or not isinstance(alias, str):
                        continue
                    if pid.strip() and alias.strip():
                        mapping[pid.strip()] = alias.strip().lower()

    return mapping


def _unwrap_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize kai-gateway / Linear webhook payload wrappers.

    Linear webhooks often wrap the event data under a top-level ``data`` key.
    kai-gateway may also wrap the original webhook body under ``payload``.

    We merge nested dict wrappers into a single dict so downstream extractors
    can consistently look for ``agentSession`` / ``agentActivity`` fields.
    """

    out: dict[str, Any] = dict(payload or {})
    for _ in range(6):
        inner_payload = out.get("payload")
        if isinstance(inner_payload, dict):
            merged = dict(out)
            merged.pop("payload", None)
            merged.update(inner_payload)
            out = merged
            continue

        data = out.get("data")
        if isinstance(data, dict):
            merged = dict(out)
            merged.pop("data", None)
            merged.update(data)
            out = merged
            continue

        break
    return out


def _normalize_event_type(event_type: object, action: object) -> str:
    raw = str(event_type or "")
    raw_lower = raw.lower()
    action_lower = action.lower() if isinstance(action, str) else None

    # Native Linear webhook format: { type: "AgentSessionEvent", action: "created" }.
    if raw == "AgentSessionEvent" or raw_lower == "agentsessionevent":
        return f"agent_session.{action_lower}" if action_lower else ""

    # kai-gateway format: { event_type: "agentsessionevent.created" } (action already embedded).
    if raw_lower.startswith("agentsessionevent."):
        return "agent_session." + raw_lower.removeprefix("agentsessionevent.")

    return raw_lower


def _extract_session_id(payload: dict[str, Any]) -> str | None:
    agent_session = payload.get("agentSession") or payload.get("agent_session")
    if isinstance(agent_session, dict):
        sid = agent_session.get("id")
        if isinstance(sid, str) and sid:
            return sid
    sid = payload.get("agentSessionId") or payload.get("agent_session_id")
    if isinstance(sid, str) and sid:
        return sid
    sid = payload.get("id")
    if isinstance(sid, str) and sid and "issue" in payload:
        return sid
    return None


def _extract_issue_title(payload: dict[str, Any]) -> str | None:
    agent_session = payload.get("agentSession") or payload.get("agent_session")
    if isinstance(agent_session, dict):
        issue = agent_session.get("issue")
        if isinstance(issue, dict):
            title = issue.get("title")
            if isinstance(title, str) and title.strip():
                return title.strip()
    issue = payload.get("issue")
    if isinstance(issue, dict):
        title = issue.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    title = payload.get("issueTitle") or payload.get("issue_title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None


_PROMPT_CONTEXT_TITLE_RE = re.compile(r"<title>(?P<title>[^<]+)</title>", re.IGNORECASE)


def _extract_issue_title_from_prompt_context(payload: dict[str, Any]) -> str | None:
    prompt_context = payload.get("promptContext") or payload.get("prompt_context")
    if not isinstance(prompt_context, str) or not prompt_context.strip():
        return None
    match = _PROMPT_CONTEXT_TITLE_RE.search(prompt_context)
    if match:
        title = match.group("title").strip()
        if title:
            return title
    return None


def _extract_issue_project_id(payload: dict[str, Any]) -> str | None:
    agent_session = payload.get("agentSession") or payload.get("agent_session")
    if isinstance(agent_session, dict):
        issue = agent_session.get("issue")
        if isinstance(issue, dict):
            project = issue.get("project")
            if isinstance(project, dict):
                pid = project.get("id")
                if isinstance(pid, str) and pid.strip():
                    return pid.strip()
            pid = issue.get("projectId") or issue.get("project_id")
            if isinstance(pid, str) and pid.strip():
                return pid.strip()
    return None


def _extract_issue_id(payload: dict[str, Any]) -> str | None:
    agent_session = payload.get("agentSession") or payload.get("agent_session")
    if isinstance(agent_session, dict):
        issue = agent_session.get("issue")
        if isinstance(issue, dict):
            iid = issue.get("id") or issue.get("issueId") or issue.get("issue_id")
            if isinstance(iid, str) and iid.strip():
                return iid.strip()
    issue = payload.get("issue")
    if isinstance(issue, dict):
        iid = issue.get("id")
        if isinstance(iid, str) and iid.strip():
            return iid.strip()
    iid = payload.get("issueId") or payload.get("issue_id")
    if isinstance(iid, str) and iid.strip():
        return iid.strip()
    return None


def _coerce_text(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _extract_text_from_activity_content(content: dict[str, Any]) -> str | None:
    body = _coerce_text(content.get("body")) or _coerce_text(content.get("text"))
    if body is not None:
        return body

    # Linear SDK content payloads often nest the actual body under the content type,
    # e.g. {"type": "message", "message": {"body": "..."} }.
    for key in ("prompt", "message", "thought", "elicitation", "response", "error"):
        nested = content.get(key)
        if isinstance(nested, dict):
            nested_body = _coerce_text(nested.get("body")) or _coerce_text(nested.get("text"))
            if nested_body is not None:
                return nested_body

    action = content.get("action")
    parameter = content.get("parameter")
    if isinstance(action, dict):
        nested_action = _coerce_text(action.get("action")) or _coerce_text(action.get("type"))
        nested_parameter = (
            _coerce_text(action.get("parameter"))
            or _coerce_text(action.get("body"))
            or _coerce_text(action.get("text"))
        )
        if nested_action == "message" and nested_parameter is not None:
            return nested_parameter
    else:
        action_text = _coerce_text(action)
        parameter_text = _coerce_text(parameter)
        if action_text == "message" and parameter_text is not None:
            return parameter_text

    return None


def _extract_activity_body(agent_activity: object) -> str | None:
    if isinstance(agent_activity, str):
        return _coerce_text(agent_activity)
    if not isinstance(agent_activity, dict):
        return None

    body = _coerce_text(agent_activity.get("body"))
    if body is not None:
        return body

    content: object = agent_activity.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            content = None

    if isinstance(content, dict):
        body = _extract_text_from_activity_content(content)
        if body is not None:
            return body

    return None


def _extract_prompt_body(payload: dict[str, Any]) -> str | None:
    # For `prompted` session events, Linear includes the user's message in agentActivity.body.
    agent_activity = payload.get("agentActivity") or payload.get("agent_activity")
    body = _extract_activity_body(agent_activity)
    if body is not None:
        return body
    # Fallbacks
    prompt_context = payload.get("promptContext") or payload.get("prompt_context")
    body = _coerce_text(prompt_context)
    if body is not None:
        return body
    if isinstance(prompt_context, dict):
        body = _coerce_text(prompt_context.get("body")) or _coerce_text(prompt_context.get("text"))
        if body is not None:
            return body
    return None


def _extract_agent_activity_id(payload: dict[str, Any]) -> str | None:
    agent_activity = payload.get("agentActivity") or payload.get("agent_activity")
    if isinstance(agent_activity, dict):
        aid = agent_activity.get("id") or agent_activity.get("agentActivityId") or agent_activity.get("agent_activity_id")
        if isinstance(aid, str) and aid.strip():
            return aid.strip()
    aid = payload.get("agentActivityId") or payload.get("agent_activity_id")
    if isinstance(aid, str) and aid.strip():
        return aid.strip()
    return None


async def _maybe_fetch_prompt_from_linear(
    payload: dict[str, Any], *, client: LinearClient
) -> str | None:
    activity_id = _extract_agent_activity_id(payload)
    if not activity_id:
        return None
    try:
        activity = await client.get_agent_activity(activity_id)
    except LinearApiError:
        return None
    return _extract_activity_body(activity)


@dataclass(slots=True)
class _SessionState:
    resume: ResumeToken | None = None
    context: RunContext | None = None
    stop_requested: bool = False
    run_lock: Any = field(default_factory=anyio.Lock)
    running_tasks: RunningTasks = field(default_factory=dict)


def _request_cancel(state: _SessionState) -> int:
    tasks = list(state.running_tasks.values())
    for task in tasks:
        task.cancel_requested.set()
    return len(tasks)


async def _run_engine_for_session(
    *,
    exec_cfg: ExecBridgeConfig,
    runtime: TransportRuntime,
    session_id: str,
    user_msg_id: str,
    text: str,
    state: _SessionState,
    default_engine_override: str | None,
) -> None:
    resolved = runtime.resolve_message(
        text=text,
        reply_text=None,
        ambient_context=state.context,
        chat_id=None,
    )
    engine_override = resolved.engine_override
    if engine_override is None and default_engine_override is not None:
        engine_override = default_engine_override

    resume = resolved.resume_token or state.resume
    context = resolved.context

    try:
        entry = runtime.resolve_runner(
            resume_token=resume,
            engine_override=engine_override,
        )
    except RunnerUnavailableError as exc:
        await exec_cfg.transport.send(
            channel_id=session_id,
            message=RenderedMessage(text=f"error:\n{exc}", extra={"activity_type": "error"}),
        )
        return
    runner: Runner = entry.runner

    if not entry.available:
        reason = entry.issue or "engine unavailable"
        await exec_cfg.transport.send(
            channel_id=session_id,
            message=RenderedMessage(text=f"error:\n{reason}", extra={"activity_type": "error"}),
        )
        return

    try:
        cwd = runtime.resolve_run_cwd(context)
    except ConfigError as exc:
        await exec_cfg.transport.send(
            channel_id=session_id,
            message=RenderedMessage(text=f"error:\n{exc}", extra={"activity_type": "error"}),
        )
        return

    # Hide resume tokens in Linear UI; we keep them in memory per session.
    runner = _ResumeLineProxy(runner=runner)

    run_base_token = set_run_base_dir(cwd)
    running_tasks = state.running_tasks

    async def on_thread_known(resume_token: ResumeToken, _done: anyio.Event) -> None:
        state.resume = resume_token

    try:
        bind_run_context(
            transport="linear",
            channel_id=session_id,
            user_msg_id=user_msg_id,
            engine=runner.engine,
            resume=resume.value if resume else None,
            project=context.project if context else None,
            branch=context.branch if context else None,
            cwd=str(cwd) if cwd is not None else None,
        )
        incoming = IncomingMessage(
            channel_id=session_id,
            message_id=user_msg_id,
            text=resolved.prompt,
        )
        context_line = runtime.format_context_line(context)
        run_done = anyio.Event()

        async def run_handle() -> None:
            try:
                await handle_message(
                    exec_cfg,
                    runner=runner,
                    incoming=incoming,
                    resume_token=resume,
                    context=context,
                    context_line=context_line,
                    strip_resume_line=runtime.is_resume_line,
                    running_tasks=running_tasks,
                    on_thread_known=on_thread_known,
                )
            finally:
                run_done.set()

        async def watch_stop() -> None:
            while not run_done.is_set():
                if state.stop_requested:
                    if _request_cancel(state):
                        return
                await anyio.sleep(0.05)

        async with anyio.create_task_group() as tg:
            tg.start_soon(watch_stop)
            tg.start_soon(run_handle)
        state.context = context
    finally:
        reset_run_base_dir(run_base_token)
        clear_context()


@dataclass(slots=True)
class _ResumeLineProxy:
    runner: Runner

    @property
    def engine(self) -> str:
        return self.runner.engine

    def is_resume_line(self, line: str) -> bool:
        return self.runner.is_resume_line(line)

    def format_resume(self, _: ResumeToken) -> str:
        return ""

    def extract_resume(self, text: str | None) -> ResumeToken | None:
        return self.runner.extract_resume(text)

    def run(self, prompt: str, resume: ResumeToken | None):
        return self.runner.run(prompt, resume)


async def _run_loop(
    *,
    settings: LinearTransportSettings,
    runtime: TransportRuntime,
    exec_cfg: ExecBridgeConfig,
    default_engine_override: str | None,
    config_path: Path,
) -> None:
    client = cast(LinearClient, getattr(exec_cfg.transport, "_client", None))
    project_map = _load_linear_project_map(config_path)
    sessions: dict[str, _SessionState] = {}

    poller = GatewayPoller(
        database_url=settings.gateway_database_url,
        source=settings.source,
        batch_size=settings.poll_batch_size,
    )

    async with poller:
        async with anyio.create_task_group() as tg:

            async def handle_and_mark(event: GatewayEvent) -> None:
                try:
                    await _handle_event(
                        event=event,
                        runtime=runtime,
                        exec_cfg=exec_cfg,
                        client=client,
                        settings=settings,
                        project_map=project_map,
                        sessions=sessions,
                        default_engine_override=default_engine_override,
                    )
                except Exception as exc:
                    logger.exception(
                        "event.failed",
                        event_id=event.id,
                        event_type=event.event_type,
                        error=str(exc),
                        error_type=exc.__class__.__name__,
                    )
                    try:
                        await poller.mark_failed(event.id, error=str(exc))
                    except Exception:
                        logger.exception(
                            "event.mark_failed_failed",
                            event_id=event.id,
                            event_type=event.event_type,
                        )
                    return

                try:
                    await poller.mark_done(event.id)
                except Exception:
                    logger.exception(
                        "event.mark_done_failed",
                        event_id=event.id,
                        event_type=event.event_type,
                    )

            while True:
                events = await poller.poll()
                if not events:
                    await poller.sleep(settings.poll_interval)
                    continue
                for event in events:
                    tg.start_soon(handle_and_mark, event)


async def _handle_event(
    *,
    event: GatewayEvent,
    runtime: TransportRuntime,
    exec_cfg: ExecBridgeConfig,
    client: LinearClient,
    settings: LinearTransportSettings,
    project_map: dict[str, str],
    sessions: dict[str, _SessionState],
    default_engine_override: str | None,
) -> None:
    _ = settings
    payload = _unwrap_payload(event.payload)

    event_type = event.event_type or payload.get("event_type") or payload.get("type")
    action = payload.get("action")

    normalized = _normalize_event_type(event_type, action)

    if normalized not in {"agent_session.created", "agent_session.prompted", *_STOP_SESSION_EVENTS}:
        logger.debug("event.ignored", event_id=event.id, event_type=normalized)
        return

    session_id = _extract_session_id(payload)
    if not session_id:
        raise RuntimeError(f"Missing agent session id in event payload: {event.payload!r}")

    state = sessions.setdefault(session_id, _SessionState())

    if normalized in _STOP_SESSION_EVENTS:
        state.stop_requested = True
        cancelled = _request_cancel(state)
        run_in_flight = False
        try:
            run_in_flight = bool(state.run_lock.locked())
        except Exception:
            run_in_flight = False

        logger.info(
            "session.stop_requested",
            session_id=session_id,
            cancelled_tasks=cancelled,
            run_in_flight=run_in_flight,
            event_id=event.id,
            event_type=normalized,
        )
        if cancelled or run_in_flight:
            await exec_cfg.transport.send(
                channel_id=session_id,
                message=RenderedMessage(
                    text="Stop requested. Cancelling…",
                    extra={"activity_type": "thought", "ephemeral": True},
                ),
            )
        return

    async with state.run_lock:
        state.stop_requested = False

        if normalized == "agent_session.created":
            steps: list[PlanStep] = [
                {"content": "Analyze request", "status": "inProgress"},
                {"content": "Implement changes", "status": "pending"},
                {"content": "Run tests", "status": "pending"},
                {"content": "Summarize results", "status": "pending"},
            ]
            try:
                await client.set_agent_plan(session_id=session_id, steps=steps)
            except (LinearApiError, Exception):
                logger.debug("plan.set_failed", session_id=session_id)

        issue_id: str | None = None
        issue_from_api: dict[str, Any] | None = None

        project_id = _extract_issue_project_id(payload)
        issue_title = _extract_issue_title(payload) or _extract_issue_title_from_prompt_context(payload)

        if normalized == "agent_session.created":
            issue_id = _extract_issue_id(payload)
            if issue_id is not None and (project_id is None or issue_title is None):
                try:
                    issue_from_api = await client.get_issue(issue_id)
                except LinearApiError:
                    issue_from_api = None

            if project_id is None and issue_from_api is not None:
                project = issue_from_api.get("project")
                if isinstance(project, dict):
                    project_id = _coerce_text(project.get("id")) or project_id

        if state.context is None and project_id and project_id in project_map:
            state.context = RunContext(project=project_map[project_id], branch=None)

        if normalized == "agent_session.created":
            if issue_title is None and issue_from_api is not None:
                issue_title = _coerce_text(issue_from_api.get("title")) or issue_title
            prompt = issue_title or _extract_prompt_body(payload)
        else:
            prompt = _extract_prompt_body(payload)

        if not prompt:
            prompt = await _maybe_fetch_prompt_from_linear(payload, client=client)

        if not prompt:
            msg = (
                "error:\nMissing prompt text in webhook payload. "
                "Ensure kai-gateway forwards Linear's agentActivity body/content."
            )
            try:
                await exec_cfg.transport.send(
                    channel_id=session_id,
                    message=RenderedMessage(text=msg, extra={"activity_type": "error"}),
                )
            finally:
                raise RuntimeError(f"Missing prompt text in event payload: {event.payload!r}")

        bind_run_context(
            event_id=event.id,
            event_type=normalized,
            session_id=session_id,
        )
        await exec_cfg.transport.send(
            channel_id=session_id,
            message=RenderedMessage(
                text="Acknowledged. Starting…",
                extra={"activity_type": "thought", "ephemeral": True},
            ),
        )

        if state.stop_requested:
            logger.info(
                "session.stop_before_run",
                session_id=session_id,
                event_id=event.id,
                event_type=normalized,
            )
            return

        await _run_engine_for_session(
            exec_cfg=exec_cfg,
            runtime=runtime,
            session_id=session_id,
            user_msg_id=event.id,
            text=prompt,
            state=state,
            default_engine_override=default_engine_override,
        )

        if normalized == "agent_session.created" and not state.stop_requested:
            try:
                await client.set_agent_plan(
                    session_id=session_id,
                    steps=[
                        {"content": "Analyze request", "status": "completed"},
                        {"content": "Implement changes", "status": "completed"},
                        {"content": "Run tests", "status": "completed"},
                        {"content": "Summarize results", "status": "completed"},
                    ],
                )
            except (LinearApiError, Exception):
                logger.debug("plan.finalize_failed", session_id=session_id)


class LinearBackend(TransportBackend):
    id = "linear"
    description = "Linear agent transport"

    def check_setup(
        self,
        engine_backend: EngineBackend,
        *,
        transport_override: str | None = None,
    ) -> SetupResult:
        issues: list[SetupIssue] = []
        config_path = _resolve_takopi_config_path()

        cmd = engine_backend.cli_cmd or engine_backend.id
        if shutil.which(cmd) is None:
            issues.append(install_issue(cmd, engine_backend.install_cmd))

        try:
            raw = read_config(config_path)
        except ConfigError:
            issues.append(_config_issue(config_path, title="create a config"))
            return SetupResult(issues=issues, config_path=config_path)

        transports = raw.get("transports")
        linear = transports.get("linear") if isinstance(transports, dict) else None
        if not isinstance(linear, dict):
            issues.append(_config_issue(config_path, title="configure transports.linear"))
            return SetupResult(issues=issues, config_path=config_path)

        missing = [k for k in ("oauth_token", "app_id", "gateway_database_url") if not linear.get(k)]
        if missing:
            issues.append(
                SetupIssue(
                    "missing required transports.linear keys",
                    tuple(f"   - {key}" for key in missing),
                )
            )

        return SetupResult(issues=issues, config_path=config_path)

    async def interactive_setup(self, *, force: bool) -> bool:
        return False

    def lock_token(self, *, transport_config: object, _config_path: Path) -> str | None:
        settings = _expect_settings(transport_config)
        return settings.oauth_token

    def build_and_run(
        self,
        *,
        transport_config: object,
        config_path: Path,
        runtime: TransportRuntime,
        final_notify: bool,
        default_engine_override: str | None,
    ) -> None:
        settings = _expect_settings(transport_config)
        client = LinearClient(settings.oauth_token, api_url=settings.api_url)
        transport = LinearTransport(client)
        presenter = LinearPresenter(
            message_overflow=settings.message_overflow,
            max_body_chars=settings.max_body_chars,
        )
        exec_cfg = ExecBridgeConfig(
            transport=transport,
            presenter=presenter,
            final_notify=final_notify,
        )

        async def run() -> None:
            await _run_loop(
                settings=settings,
                runtime=runtime,
                exec_cfg=exec_cfg,
                default_engine_override=default_engine_override,
                config_path=config_path,
            )

        anyio.run(run)


linear_backend = LinearBackend()
BACKEND = linear_backend
