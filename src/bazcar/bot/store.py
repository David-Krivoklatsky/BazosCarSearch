"""User state storage for the Telegram bot (Phase 4): prefs, searches, saved.

Uses the same Neon Postgres as listing persistence. Tables are created by the
shared schema init in ``ListingRepository.init_schema``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from bazcar.core.exceptions import DbError

logger = logging.getLogger(__name__)


@dataclass
class UserPrefs:
    chat_id: int
    criteria: str | None = None
    model: str | None = None
    min_score: int | None = None
    show_photo: bool = False


@dataclass
class SearchProfile:
    name: str
    criteria: str


@dataclass
class SavedListing:
    ad_id: int
    ad_title: str | None
    ad_url: str | None


class UserStore:
    """Owns a single asyncpg connection and provides bot persistence."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: asyncpg.Connection | None = None

    async def connect(self) -> None:
        try:
            self._conn = await asyncpg.connect(self._dsn)
        except (OSError, asyncpg.PostgresError) as exc:
            raise DbError(f"cannot connect to database: {exc}") from exc

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> UserStore:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    def _require(self) -> asyncpg.Connection:
        if self._conn is None:
            raise DbError("user store is not connected; call connect() first")
        return self._conn

    # ------------------------------------------------------------- preferences

    async def get_prefs(self, chat_id: int) -> UserPrefs | None:
        row = await self._require().fetchrow(
            "SELECT chat_id, criteria, model, min_score, show_photo"
            " FROM user_prefs WHERE chat_id = $1",
            chat_id,
        )
        return UserPrefs(**dict(row)) if row else None

    async def ensure_prefs(self, chat_id: int) -> UserPrefs:
        prefs = await self.get_prefs(chat_id)
        if prefs is not None:
            return prefs
        await self._require().execute(
            "INSERT INTO user_prefs (chat_id) VALUES ($1) ON CONFLICT (chat_id) DO NOTHING",
            chat_id,
        )
        return UserPrefs(chat_id=chat_id)

    async def update_prefs(self, chat_id: int, **fields) -> None:
        allowed = {"criteria", "model", "min_score", "show_photo"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        columns = ", ".join(f"{key} = ${i + 1}" for i, key in enumerate(updates))
        values = list(updates.values()) + [chat_id]
        await self._require().execute(
            f"UPDATE user_prefs SET {columns}, updated_at = now() WHERE chat_id = ${len(values)}",
            *values,
        )

    # --------------------------------------------------------------- searches

    async def list_searches(self, chat_id: int) -> list[SearchProfile]:
        rows = await self._require().fetch(
            "SELECT name, criteria FROM user_searches WHERE chat_id = $1 ORDER BY created_at",
            chat_id,
        )
        return [SearchProfile(name=row["name"], criteria=row["criteria"]) for row in rows]

    async def save_search(self, chat_id: int, name: str, criteria: str) -> None:
        await self._require().execute(
            "INSERT INTO user_searches (chat_id, name, criteria) VALUES ($1, $2, $3)"
            " ON CONFLICT (chat_id, name) DO UPDATE SET criteria = EXCLUDED.criteria,"
            " updated_at = now()",
            chat_id,
            name,
            criteria,
        )

    async def delete_search(self, chat_id: int, name: str) -> bool:
        result = await self._require().execute(
            "DELETE FROM user_searches WHERE chat_id = $1 AND name = $2",
            chat_id,
            name,
        )
        return result != "DELETE 0"

    # ---------------------------------------------------------- saved listings

    async def save_listing(self, chat_id: int, ad_id: int, title: str | None, url: str | None) -> None:
        await self._require().execute(
            "INSERT INTO saved_listings (chat_id, ad_id, ad_title, ad_url)"
            " VALUES ($1, $2, $3, $4) ON CONFLICT (chat_id, ad_id) DO NOTHING",
            chat_id,
            ad_id,
            title,
            url,
        )

    async def list_saved(self, chat_id: int) -> list[SavedListing]:
        rows = await self._require().fetch(
            "SELECT ad_id, ad_title, ad_url FROM saved_listings"
            " WHERE chat_id = $1 ORDER BY saved_at DESC",
            chat_id,
        )
        return [SavedListing(ad_id=r["ad_id"], ad_title=r["ad_title"], ad_url=r["ad_url"]) for r in rows]

    async def remove_saved(self, chat_id: int, ad_id: int) -> bool:
        result = await self._require().execute(
            "DELETE FROM saved_listings WHERE chat_id = $1 AND ad_id = $2",
            chat_id,
            ad_id,
        )
        return result != "DELETE 0"
