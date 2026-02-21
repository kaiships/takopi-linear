from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LinearTransportSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    oauth_token: str = Field(..., description="Linear OAuth access token (actor=app)")
    app_id: str = Field(..., description="Linear app id")
    gateway_database_url: str = Field(..., description="Postgres URL for kai-gateway")

    api_url: str = Field(
        default="https://api.linear.app/graphql",
        description="Linear GraphQL API URL",
    )

    source: str = Field(default="linear", description="Gateway events source filter")
    poll_interval: float = Field(default=5.0, ge=0.5)
    poll_batch_size: int = Field(default=10, ge=1, le=100)

    message_overflow: Literal["trim", "split"] = "split"
    max_body_chars: int = Field(default=10_000, ge=500)

    @field_validator("oauth_token", "app_id", "gateway_database_url")
    @classmethod
    def _require_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("gateway_database_url")
    @classmethod
    def _validate_gateway_db_url(cls, value: str) -> str:
        if not (
            value.startswith("postgres://")
            or value.startswith("postgresql://")
            or value.startswith("postgresql+psycopg://")
        ):
            raise ValueError("must be a postgres connection URL")
        return value

