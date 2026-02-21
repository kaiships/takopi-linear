from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import anyio

from .types import GatewayEvent


def _require_psycopg() -> tuple[Any, Any]:
    try:
        import psycopg  # type: ignore[import-not-found]
        from psycopg.rows import dict_row  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "psycopg is required to poll the kai-gateway database; install "
            "`takopi-linear` with psycopg[binary]."
        ) from exc
    return psycopg, dict_row


_CLAIM_SQL = """
UPDATE events
SET status = 'processing', processed_at = now()
WHERE id IN (
  SELECT id FROM events
  WHERE source = %s AND status = 'pending'
  ORDER BY created_at
  LIMIT %s
  FOR UPDATE SKIP LOCKED
)
RETURNING id, source, event_type, external_id, payload, created_at
"""

_DONE_SQL = "UPDATE events SET status = 'done', processed_at = now() WHERE id = %s"

_FAILED_SQL = """
UPDATE events
SET status = 'failed', processed_at = now(), error = %s
WHERE id = %s
"""


class GatewayPoller:
    def __init__(
        self,
        *,
        database_url: str,
        source: str = "linear",
        batch_size: int = 10,
        sleep: Callable[[float], Any] = anyio.sleep,
        conn: Any | None = None,
    ) -> None:
        self._database_url = database_url
        self._source = source
        self._batch_size = int(batch_size)
        self._sleep = sleep
        self._lock = anyio.Lock()
        self._conn: Any | None = conn
        self._own_conn = conn is None

    async def open(self) -> None:
        if self._conn is not None:
            return
        psycopg, dict_row = _require_psycopg()
        self._conn = await psycopg.AsyncConnection.connect(
            self._database_url,
            row_factory=dict_row,
        )

    async def close(self) -> None:
        if self._conn is None:
            return
        if self._own_conn:
            await self._conn.close()
        self._conn = None

    async def __aenter__(self) -> GatewayPoller:
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def poll(self) -> list[GatewayEvent]:
        async with self._lock:
            if self._conn is None:
                await self.open()
            conn = cast(Any, self._conn)
            async with conn.cursor() as cur:
                await cur.execute(_CLAIM_SQL, (self._source, self._batch_size))
                rows = await cur.fetchall()
            await conn.commit()
        events: list[GatewayEvent] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            events.append(
                GatewayEvent(
                    id=str(row.get("id", "")),
                    source=str(row.get("source", "")),
                    event_type=str(row.get("event_type", "")),
                    external_id=(str(row["external_id"]) if row.get("external_id") else None),
                    payload=payload,
                    created_at=row.get("created_at"),
                )
            )
        return events

    async def mark_done(self, event_id: str) -> None:
        async with self._lock:
            if self._conn is None:
                await self.open()
            conn = cast(Any, self._conn)
            async with conn.cursor() as cur:
                await cur.execute(_DONE_SQL, (event_id,))
            await conn.commit()

    async def mark_failed(self, event_id: str, *, error: str) -> None:
        async with self._lock:
            if self._conn is None:
                await self.open()
            conn = cast(Any, self._conn)
            async with conn.cursor() as cur:
                await cur.execute(_FAILED_SQL, (error, event_id))
            await conn.commit()

    async def sleep(self, seconds: float) -> None:
        await self._sleep(seconds)
