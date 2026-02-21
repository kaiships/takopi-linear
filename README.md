# takopi-linear

Linear transport backend plugin for [takopi](https://github.com/kaiships/takopi).

This repository is currently under active development; see `PLAN.md` for the intended
architecture and lifecycle.

## Config sketch

```toml
transport = "linear"

[transports.linear]
oauth_token = "..."
app_id = "..."
gateway_database_url = "postgresql://.../kai_gateway?sslmode=require"
poll_interval = 5.0
message_overflow = "split"

# Optional: map Linear project id -> takopi project alias
[plugins.linear]
project_map = { "linear-project-id" = "backend" }
```

