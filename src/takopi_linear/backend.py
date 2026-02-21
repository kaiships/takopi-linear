from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import anyio

from takopi.backends import EngineBackend, SetupIssue
from takopi.backends_helpers import install_issue
from takopi.config import ConfigError, read_config, resolve_config_path
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
    data = payload.get("data")
    if isinstance(data, dict) and any(key in data for key in ("agentSession", "agentActivity", "promptContext")):
        return data
    return payload


def _extract_session_id(payload: dict[str, Any]) -> str | None:
    agent_session = payload.get("agentSession")
    if isinstance(agent_session, dict):
        sid = agent_session.get("id")
        if isinstance(sid, str) and sid:
            return sid
    sid = payload.get("agentSessionId")
    if isinstance(sid, str) and sid:
        return sid
    sid = payload.get("id")
    if isinstance(sid, str) and sid and "issue" in payload:
        return sid
    return None


def _extract_issue_title(payload: dict[str, Any]) -> str | None:
    agent_session = payload.get("agentSession")
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
    return None


def _extract_issue_project_id(payload: dict[str, Any]) -> str | None:
    agent_session = payload.get("agentSession")
    if isinstance(agent_session, dict):
        issue = agent_session.get("issue")
        if isinstance(issue, dict):
            project = issue.get("project")
            if isinstance(project, dict):
                pid = project.get("id")
                if isinstance(pid, str) and pid.strip():
                    return pid.strip()
            pid = issue.get("projectId")
            if isinstance(pid, str) and pid.strip():
                return pid.strip()
    return None


def _extract_prompt_body(payload: dict[str, Any]) -> str | None:
    # For `prompted` session events, Linear includes the user's message in agentActivity.body.
    agent_activity = payload.get("agentActivity")
    if isinstance(agent_activity, dict):
        body = agent_activity.get("body")
        if isinstance(body, str) and body.strip():
            return body.strip()
    # Fallbacks
    body = payload.get("promptContext")
    if isinstance(body, str) and body.strip():
        return body.strip()
    return None


@dataclass(slots=True)
class _SessionState:
    resume: ResumeToken | None = None
    context: RunContext | None = None


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
    running_tasks: RunningTasks = {}

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
        while True:
            events = await poller.poll()
            if not events:
                await poller.sleep(settings.poll_interval)
                continue
            for event in events:
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
                    await poller.mark_done(event.id)
                except Exception as exc:
                    logger.exception(
                        "event.failed",
                        event_id=event.id,
                        event_type=event.event_type,
                        error=str(exc),
                        error_type=exc.__class__.__name__,
                    )
                    await poller.mark_failed(event.id, error=str(exc))


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
    payload = _unwrap_payload(event.payload)

    event_type = event.event_type or payload.get("event_type") or payload.get("type")
    action = payload.get("action")

    normalized = str(event_type or "")
    if normalized == "AgentSessionEvent":
        normalized = f"agent_session.{action}" if isinstance(action, str) else ""

    if normalized not in {"agent_session.created", "agent_session.prompted"}:
        logger.debug("event.ignored", event_id=event.id, event_type=normalized)
        return

    session_id = _extract_session_id(payload)
    if not session_id:
        raise RuntimeError(f"Missing agent session id in event payload: {event.payload!r}")

    state = sessions.setdefault(session_id, _SessionState())

    # Set a simple Agent Plan at session start (best-effort).
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

    project_id = _extract_issue_project_id(payload)
    if state.context is None and project_id and project_id in project_map:
        state.context = RunContext(project=project_map[project_id], branch=None)

    if normalized == "agent_session.created":
        prompt = _extract_issue_title(payload) or _extract_prompt_body(payload) or "continue"
    else:
        prompt = _extract_prompt_body(payload) or "continue"

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

    await _run_engine_for_session(
        exec_cfg=exec_cfg,
        runtime=runtime,
        session_id=session_id,
        user_msg_id=event.id,
        text=prompt,
        state=state,
        default_engine_override=default_engine_override,
    )

    if normalized == "agent_session.created":
        # Best-effort: mark plan completed at the end of the first run.
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
        config_path = resolve_config_path()

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
