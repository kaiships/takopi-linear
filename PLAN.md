# Takopi Linear Transport Plugin - Implementation Plan

## Overview

Create a takopi transport plugin that integrates with Linear's Agent SDK, where:
- **Kai is a Linear bot** with its own identity (OAuth `actor=app`)
- **One Linear issue = one takopi session**
- **Issue title = prompt** (with optional `@branch` prefix for branch/worktree targeting)
- **Linear project = repo mapping** (via per-project `linear_project_id` config)
- Communication uses Linear's native Agent Activities (thoughts, actions, responses)

## Lifecycle

```
┌──────────────────────────────────────────────────────────────────────┐
│  1. DELEGATION                                                       │
│                                                                      │
│  User creates issue: "@feat/auth Add JWT authentication to API"      │
│  User delegates to Kai (assigns as agent)                            │
│  → Linear fires AgentSessionEvent (created)                          │
│  → Kai acknowledges with Thought within 10s                          │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  2. PLANNING (Backlog)                                               │
│                                                                      │
│  Kai parses title: branch=feat/auth, prompt="Add JWT auth to API"    │
│  Kai analyzes codebase, emits Thoughts as it reasons                 │
│  Kai sets Agent Plan with implementation steps                       │
│  Kai emits Elicitation: "Ready to proceed?"                          │
│  User replies → AgentSessionEvent (prompted)                         │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    │  User moves issue to Todo
                                    │  (or replies with approval)
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  3. EXECUTION (In Progress)                                          │
│                                                                      │
│  Kai creates branch feat/auth + worktree (if @branch specified)      │
│  Kai implements the plan, updating Plan steps as it goes             │
│  Kai emits Actions: "Created file auth.py", "Running tests"         │
│  Kai creates PR (branch named for Linear auto-link: TEAM-123-...)    │
│  Kai moves issue to In Review                                        │
│  Kai emits Action: "PR #42 created"                                  │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  4. REVIEW (In Review)                                               │
│                                                                      │
│  PR is open, linked to issue                                         │
│  User comments on issue → AgentSessionEvent (prompted)               │
│  Kai responds, pushes fixes, updates Plan                            │
│  Back-and-forth until ready                                          │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    │  PR merged → Linear auto-moves to Done
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  5. DONE                                                             │
│                                                                      │
│  Linear auto-moves issue when PR merges (correct branch naming)      │
│  Kai emits Response (final activity, session completes)              │
│  Worktree cleaned up                                                 │
└──────────────────────────────────────────────────────────────────────┘
```

## Issue Title Syntax

```
[@branch/name] <prompt>
```

| Title | Branch | Worktree | Prompt |
|-------|--------|----------|--------|
| `Add JWT authentication` | base (main) | no | "Add JWT authentication" |
| `@feat/auth Add JWT authentication` | feat/auth | yes | "Add JWT authentication" |
| `@fix/login Fix the login bug` | fix/login | yes | "Fix the login bug" |

- **No `@branch`** → Work on base branch, no worktree
- **`@branch` specified** → Create new branch + worktree (or use existing)

## Architecture

```
takopi-linear/
├── pyproject.toml              # Package config with entry point
├── src/
│   └── takopi_linear/
│       ├── __init__.py
│       ├── backend.py          # LinearBackend (TransportBackend protocol)
│       ├── client.py           # LinearClient (GraphQL API + Agent SDK)
│       ├── bridge.py           # LinearTransport + LinearPresenter
│       ├── settings.py         # Pydantic config models
│       ├── poller.py           # Gateway DB poller for incoming events
│       └── types.py            # Type definitions
└── tests/
    └── ...
```

## Components

### 1. LinearClient (`client.py`)
GraphQL API wrapper using httpx:
- OAuth `actor=app` authentication
- Agent Session management (activities, plans, session updates)
- Issue operations (read, update state)
- Rate limiting (500 req/hr for OAuth apps)

Key methods:
```python
class LinearClient:
    # Identity
    async def get_viewer() -> User

    # Issues
    async def get_issue(issue_id: str) -> Issue
    async def update_issue(issue_id, **fields) -> Issue
    async def get_workflow_states(team_id) -> list[WorkflowState]

    # Agent SDK
    async def create_agent_activity(session_id, type, body, **kwargs) -> Activity
    async def update_agent_session(session_id, **fields) -> Session
    async def set_agent_plan(session_id, steps: list[PlanStep]) -> Session
```

### 2. Agent Activity Types

| Type | Usage | Example |
|------|-------|---------|
| `thought` | Internal reasoning, progress notes | "Analyzing codebase structure..." |
| `action` | Tool invocations, concrete steps | "Created branch feat/auth" |
| `elicitation` | Request user input | "Ready to proceed with this plan?" |
| `response` | Final result, session complete | "PR #42 ready for review" |
| `error` | Failure reporting | "Tests failed, see details below" |

### 3. Agent Plans

Replace writing plans to issue description. Linear renders these natively:
```python
await client.set_agent_plan(session_id, [
    {"content": "Analyze codebase", "status": "completed"},
    {"content": "Create auth middleware", "status": "inProgress"},
    {"content": "Add JWT token handling", "status": "pending"},
    {"content": "Write tests", "status": "pending"},
    {"content": "Create PR", "status": "pending"},
])
```

Plan step statuses: `pending` | `inProgress` | `completed` | `canceled`

### 4. LinearBackend (`backend.py`)
Implements `TransportBackend` protocol:
```python
class LinearBackend:
    id = "linear"
    description = "Linear"

    def check_setup(engine_backend, transport_override) -> SetupResult
    async def interactive_setup(force: bool) -> bool
    def lock_token(transport_config, config_path) -> str | None
    def build_and_run(transport_config, config_path, runtime, ...) -> None
```

### 5. LinearTransport (`bridge.py`)
Implements `Transport` protocol. Maps takopi messages to Agent Activities:
```python
class LinearTransport:
    async def send(channel_id, message, options) -> MessageRef
        # channel_id = agent session ID
        # Emits agent activity (thought/action/response)
        # Returns MessageRef with activity ID

    async def edit(ref, message) -> MessageRef
        # Updates existing activity or emits new one

    async def delete(ref) -> bool

    async def close() -> None
```

### 6. LinearPresenter (`bridge.py`)
Renders takopi state to Linear markdown agent activities:
```python
class LinearPresenter:
    def render_progress(state, elapsed_s, label) -> RenderedMessage
    def render_final(state, elapsed_s, status, answer) -> RenderedMessage
```

### 7. Gateway Poller (`poller.py`)
Polls the kai-gateway Neon DB for pending Linear events:
```python
class GatewayPoller:
    async def poll() -> list[Event]
    # Atomically claims pending events via FOR UPDATE SKIP LOCKED
    # Handles event types:
    #   - agent_session.created: New delegation, start planning
    #   - agent_session.prompted: User replied, continue conversation
    async def mark_done(event_id: str) -> None
    async def mark_failed(event_id: str, error: str) -> None
```

### 8. Settings (`settings.py`)
```python
class LinearTransportSettings(BaseModel):
    oauth_token: str                  # OAuth token (actor=app)
    app_id: str                       # Linear app ID
    gateway_database_url: str         # Neon DB connection string (kai-gateway)
    poll_interval: float = 5.0        # Seconds between polls

    # Comment formatting
    message_overflow: Literal["trim", "split"] = "split"
```

## Configuration

```toml
[transport]
default = "linear"

[transports.linear]
oauth_token = "..."                   # OAuth access token (actor=app)
app_id = "..."                        # Linear application ID
gateway_database_url = "postgresql://...@...neon.tech/kai_gateway?sslmode=require"
poll_interval = 5.0
message_overflow = "split"

[projects.backend]
path = "/home/deploy/backend"
linear_project_id = "project-uuid-123"

[projects.frontend]
path = "/home/deploy/frontend"
linear_project_id = "project-uuid-456"
```

## Infrastructure

### kai-gateway (separate service)
- Vercel serverless function receives Linear webhooks
- Validates signatures, normalizes events, inserts into Neon DB
- Repo: kaiships/kai-gateway
- See kai-gateway/PLAN.md for details

### Neon DB
- Shared between kai-gateway (writes) and takopi-linear (reads)
- Events table with status tracking (pending → processing → done/failed)
- Atomic claim via `FOR UPDATE SKIP LOCKED`

## Linear App Setup

1. **Create Application** in Linear Settings → API → Applications
2. **Configure OAuth** with `actor=app` and scopes:
   - `app:assignable` — Allow delegation to Kai
   - `app:mentionable` — Allow @Kai mentions
   - `read` / `write` — Issue and comment access
3. **Install to workspace** (requires admin)
4. **Configure webhook** → Point to kai-gateway Vercel URL, enable Agent session events
5. **Store credentials** in takopi config

## Session/Issue Mapping

| Linear Concept | Takopi Concept |
|----------------|----------------|
| Issue | Session context (prompt, branch info) |
| Agent Session | Active takopi session |
| Agent Activity | Message / progress update |
| Agent Plan | Implementation plan steps |
| Project | Repo (via `linear_project_id`) |
| Issue title `@branch` | Branch + worktree target |
| Delegate (assign to Kai) | Trigger session start |

MessageRef structure:
```python
MessageRef(
    channel_id="agent-session-id",
    message_id="activity-id",
    thread_id=None,
    raw={"issue_id": "issue-uuid", "issue_identifier": "TEAM-123"},
    sender_id="app-user-id"
)
```

## Branch Naming

For Linear to auto-link PRs and auto-move issues on merge:
- Branch format: `TEAM-123-slug` (e.g., `KAI-42-add-jwt-authentication`)
- If user specifies `@feat/auth`, the worktree uses that branch name
- PR still references the issue via description or Linear's branch linking

## Implementation Steps

1. **Project setup**
   - Create pyproject.toml with dependencies (httpx, pydantic, anyio)
   - Set up entry point for `takopi.transport_backends`
   - Create package structure

2. **LinearClient**
   - Implement GraphQL client with httpx
   - OAuth `actor=app` authentication
   - Agent SDK methods (activities, plans, session updates)
   - Issue/state operations
   - Rate limiting (500 req/hr for OAuth)

3. **Settings models**
   - Pydantic models for configuration
   - Validation for OAuth token, app ID

4. **Gateway Poller**
   - Poll Neon DB for pending Linear events
   - Atomic claim with `FOR UPDATE SKIP LOCKED`
   - Mark events done/failed after processing
   - Parse issue title for `@branch` and prompt

5. **LinearBackend**
   - `check_setup()` for config validation (OAuth token, gateway DB URL)
   - `interactive_setup()` for guided setup
   - `lock_token()` for secret extraction
   - `build_and_run()` to start polling loop + transport

6. **LinearTransport**
   - Map send/edit/delete to agent activities
   - Handle message overflow (split long messages)
   - Map takopi session lifecycle to agent session lifecycle

7. **LinearPresenter**
   - Render progress as Thought activities
   - Render final as Response activities
   - Format for Linear's markdown

8. **Testing**
   - Unit tests for client methods
   - Integration tests with mock DB

## Dependencies

```toml
[project]
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.0",
    "anyio>=4.0",
    "psycopg[binary]>=3.2",
    "takopi>=0.1",  # peer dependency
]
```
