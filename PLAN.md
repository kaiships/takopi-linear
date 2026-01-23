# Takopi Linear Transport Plugin - Implementation Plan

## Overview

Create a takopi transport plugin that integrates with Linear, where:
- **One Linear issue = one takopi session/branch**
- **Messages are posted as comments on the issue**
- Comments on the issue can trigger takopi responses (bidirectional)

## Architecture

```
takopi-linear/
├── pyproject.toml              # Package config with entry point
├── src/
│   └── takopi_linear/
│       ├── __init__.py
│       ├── backend.py          # LinearBackend (TransportBackend protocol)
│       ├── client.py           # LinearClient (GraphQL API wrapper)
│       ├── bridge.py           # LinearTransport + LinearPresenter
│       ├── settings.py         # Pydantic config models
│       ├── webhook.py          # Webhook server for incoming comments
│       └── types.py            # Type definitions
└── tests/
    └── ...
```

## Components

### 1. LinearClient (`client.py`)
GraphQL API wrapper using httpx:
- Authentication via API key or OAuth token
- Create/update issues
- Create/update comments
- Fetch workflow states, teams, projects
- Rate limiting (1500 req/hr for API key, 500 for OAuth)

Key methods:
```python
class LinearClient:
    async def get_viewer() -> User
    async def get_teams() -> list[Team]
    async def get_issue(issue_id: str) -> Issue
    async def create_issue(team_id, title, description, ...) -> Issue
    async def update_issue(issue_id, **fields) -> Issue
    async def create_comment(issue_id, body) -> Comment
    async def update_comment(comment_id, body) -> Comment
    async def get_workflow_states(team_id) -> list[WorkflowState]
```

### 2. LinearBackend (`backend.py`)
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

### 3. LinearTransport (`bridge.py`)
Implements `Transport` protocol:
```python
class LinearTransport:
    async def send(channel_id, message, options) -> MessageRef
        # channel_id = issue ID
        # Creates a comment on the issue
        # Returns MessageRef with comment_id

    async def edit(ref, message) -> MessageRef
        # Updates existing comment

    async def delete(ref) -> bool
        # Deletes comment (if supported)

    async def close() -> None
```

### 4. LinearPresenter (`bridge.py`)
Renders takopi state to Linear markdown:
```python
class LinearPresenter:
    def render_progress(state, elapsed_s, label) -> RenderedMessage
    def render_final(state, elapsed_s, status, answer) -> RenderedMessage
```

### 5. WebhookServer (`webhook.py`)
HTTP server to receive Linear webhooks for incoming comments:
```python
class LinearWebhookServer:
    async def handle_webhook(request) -> Response
    # Filters for Comment events on tracked issues
    # Dispatches to takopi engine for processing
```

### 6. Settings (`settings.py`)
```python
class LinearTransportSettings(BaseModel):
    api_key: str | None = None
    oauth_token: str | None = None
    team_id: str                    # Default team for new issues
    project_id: str | None = None   # Optional default project
    webhook_secret: str | None = None
    webhook_port: int = 8080

    # Issue creation defaults
    default_state: str | None = None  # e.g., "In Progress"
    default_labels: list[str] = []

    # Comment formatting
    message_overflow: Literal["trim", "split"] = "split"
    include_metadata: bool = True     # Add timestamps, status badges
```

## Session/Issue Mapping

The `channel_id` in takopi's Transport protocol maps to a Linear issue:
- **New session**: Create a new issue, store issue ID as channel_id
- **Existing session**: Use stored issue ID to post comments
- **Thread support**: Linear doesn't have comment threads, so `thread_id` is unused

MessageRef structure:
```python
MessageRef(
    channel_id="issue-uuid",
    message_id="comment-uuid",
    thread_id=None,
    raw={"issue_identifier": "TEAM-123"},
    sender_id="user-uuid"
)
```

## Configuration Example

```toml
[transport]
default = "linear"

[transports.linear]
api_key = "lin_api_..."
team_id = "team-uuid"
project_id = "project-uuid"  # optional
webhook_port = 8080
webhook_secret = "whsec_..."
message_overflow = "split"
include_metadata = true
```

## Implementation Steps

1. **Project setup**
   - Create pyproject.toml with dependencies (httpx, pydantic, anyio)
   - Set up entry point for `takopi.transport_backends`
   - Create package structure

2. **LinearClient**
   - Implement GraphQL client with httpx
   - Add authentication handling
   - Implement core API methods (issues, comments, teams)
   - Add rate limiting

3. **Settings models**
   - Define Pydantic models for configuration
   - Validation for required fields

4. **LinearBackend**
   - Implement `check_setup()` for config validation
   - Implement `interactive_setup()` for guided configuration
   - Implement `lock_token()` for secret extraction
   - Implement `build_and_run()` to start the transport

5. **LinearTransport**
   - Implement send/edit/delete for comments
   - Handle message overflow (split long messages)
   - Map takopi concepts to Linear

6. **LinearPresenter**
   - Render progress updates (with status indicators)
   - Render final responses
   - Format for Linear's markdown flavor

7. **WebhookServer** (optional, for bidirectional)
   - HTTP server for webhook events
   - Filter and dispatch comment events
   - Signature verification

8. **Testing**
   - Unit tests for client methods
   - Integration tests with mock server

## Dependencies

```toml
[project]
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.0",
    "anyio>=4.0",
    "takopi>=0.1",  # peer dependency
]
```

## Open Questions

1. Should we support issue creation from takopi, or only attach to existing issues?
   - **Recommendation**: Support both - auto-create or specify existing issue ID

2. How to handle Linear's rate limits (1500/hr)?
   - **Recommendation**: Implement token bucket rate limiter similar to Telegram transport

3. Should webhook server be optional?
   - **Recommendation**: Yes, make it opt-in for bidirectional communication
