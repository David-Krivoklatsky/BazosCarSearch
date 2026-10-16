"""User state storage for the Telegram bot (Phase 4): prefs, searches, saved.

Uses the same Neon Postgres as listing persistence. Tables are created by the
shared schema init in ``ListingRepository.init_schema``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

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
    filters: dict = field(default_factory=dict)
    show_limit: int = 5
    notify_enabled: bool = True
    prompt_name: str = "standard"
    fallback_models: list = field(default_factory=list)


@dataclass
class SearchProfile:
    name: str
    criteria: str
    filters: dict = field(default_factory=dict)
    min_score: int | None = None
    notify_enabled: bool = True


@dataclass
class SavedListing:
    ad_id: int
    ad_title: str | None
    ad_url: str | None
    note: str = ""
    current_price: int | None = None
    max_price: int | None = None
    last_seen_at: str | None = None
    alert_below: int | None = None


class UserStore:
    """Owns a single asyncpg connection and provides bot persistence."""

    def __init__(self, dsn: str, conn: asyncpg.Connection | None = None) -> None:
        self._dsn = dsn
        self._conn = conn
        self._owns_conn = conn is None

    async def connect(self) -> None:
        if self._conn is not None:
            return
        try:
            self._conn = await asyncpg.connect(self._dsn)
        except (OSError, asyncpg.PostgresError) as exc:
            raise DbError(f"cannot connect to database: {exc}") from exc

    async def close(self) -> None:
        if self._conn is not None and self._owns_conn:
            await self._conn.close()
        if self._owns_conn:
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
            "SELECT chat_id, criteria, model, min_score, show_photo, filters,"
            " show_limit, notify_enabled, prompt_name, fallback_models"
            " FROM user_prefs WHERE chat_id = $1",
            chat_id,
        )
        if not row:
            return None
        prefs = dict(row)
        prefs["filters"] = json.loads(prefs["filters"]) if prefs["filters"] else {}
        try:
            prefs["fallback_models"] = (
                json.loads(prefs["fallback_models"]) if prefs["fallback_models"] else []
            )
        except (TypeError, ValueError):
            prefs["fallback_models"] = []
        if not prefs.get("prompt_name"):
            prefs["prompt_name"] = "standard"
        return UserPrefs(**prefs)

    async def ensure_prefs(self, chat_id: int) -> UserPrefs:
        prefs = await self.get_prefs(chat_id)
        if prefs is not None:
            return prefs
        await self._require().execute(
            "INSERT INTO user_prefs (chat_id) VALUES ($1) ON CONFLICT (chat_id) DO NOTHING",
            chat_id,
        )
        return UserPrefs(chat_id=chat_id)

    async def list_all_prefs(self) -> list[UserPrefs]:
        """Every registered user's preferences — input for the multi-user sweep."""
        rows = await self._require().fetch(
            "SELECT chat_id, criteria, model, min_score, show_photo, filters,"
            " show_limit, notify_enabled, prompt_name, fallback_models"
            " FROM user_prefs ORDER BY chat_id"
        )
        prefs_list = []
        for row in rows:
            data = dict(row)
            data["filters"] = json.loads(data["filters"]) if data["filters"] else {}
            try:
                data["fallback_models"] = (
                    json.loads(data["fallback_models"]) if data["fallback_models"] else []
                )
            except (TypeError, ValueError):
                data["fallback_models"] = []
            if not data.get("prompt_name"):
                data["prompt_name"] = "standard"
            prefs_list.append(UserPrefs(**data))
        return prefs_list

    async def update_prefs(self, chat_id: int, **fields) -> None:
        allowed = {
            "criteria",
            "model",
            "min_score",
            "show_photo",
            "filters",
            "show_limit",
            "notify_enabled",
            "prompt_name",
            "fallback_models",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        if "filters" in updates:
            updates["filters"] = json.dumps(updates["filters"], ensure_ascii=False)
        if "fallback_models" in updates:
            updates["fallback_models"] = json.dumps(
                [m for m in (updates["fallback_models"] or []) if m][:3], ensure_ascii=False
            )
        if "prompt_name" in updates:
            from bazcar.llm.provider import normalize_prompt_name

            updates["prompt_name"] = normalize_prompt_name(updates["prompt_name"])
        columns = ", ".join(f"{key} = ${i + 1}" for i, key in enumerate(updates))
        values = list(updates.values()) + [chat_id]
        await self._require().execute(
            f"UPDATE user_prefs SET {columns}, updated_at = now() WHERE chat_id = ${len(values)}",
            *values,
        )

    async def update_filters(self, chat_id: int, patch: dict) -> dict:
        """Merge ``patch`` into the stored Bazoš filters; returns the new dict.

        Empty values ("") remove the key, so ``{"query": ""}`` clears the query.
        """
        prefs = await self.ensure_prefs(chat_id)
        filters = dict(prefs.filters)
        for key, value in patch.items():
            if value in (None, ""):
                filters.pop(key, None)
            else:
                filters[key] = value
        await self.update_prefs(chat_id, filters=filters)
        return filters

    async def claim_update(self, update_id: int) -> bool:
        """Atomically claim a Telegram update (True = first delivery).

        Telegram webhook delivers at-least-once; this upsert only advances the
        stored ``last_update_id``, so redelivered/older updates return False.
        """
        row = await self._require().fetchrow(
            """
            INSERT INTO bot_state (key, value) VALUES ('last_update_id', $1)
            ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = now()
            WHERE bot_state.value < EXCLUDED.value
            RETURNING value
            """,
            update_id,
        )
        return row is not None

    # --------------------------------------------------------------- searches

    async def list_searches(self, chat_id: int) -> list[SearchProfile]:
        rows = await self._require().fetch(
            "SELECT name, criteria, filters, min_score, notify_enabled FROM user_searches"
            " WHERE chat_id = $1 ORDER BY created_at",
            chat_id,
        )
        profiles = []
        for row in rows:
            profiles.append(
                SearchProfile(
                    name=row["name"],
                    criteria=row["criteria"],
                    filters=json.loads(row["filters"]) if row["filters"] else {},
                    min_score=row["min_score"],
                    notify_enabled=bool(row["notify_enabled"])
                    if row["notify_enabled"] is not None
                    else True,
                )
            )
        return profiles

    async def save_search(
        self,
        chat_id: int,
        name: str,
        criteria: str,
        filters: dict | None = None,
        min_score: int | None = None,
        notify_enabled: bool | None = None,
    ) -> None:
        if notify_enabled is None:
            await self._require().execute(
                "INSERT INTO user_searches (chat_id, name, criteria, filters, min_score)"
                " VALUES ($1, $2, $3, $4::jsonb, $5)"
                " ON CONFLICT (chat_id, name) DO UPDATE SET criteria = EXCLUDED.criteria,"
                " filters = EXCLUDED.filters, min_score = EXCLUDED.min_score,"
                " updated_at = now()",
                chat_id,
                name,
                criteria,
                json.dumps(filters or {}, ensure_ascii=False),
                min_score,
            )
            return
        await self._require().execute(
            "INSERT INTO user_searches"
            " (chat_id, name, criteria, filters, min_score, notify_enabled)"
            " VALUES ($1, $2, $3, $4::jsonb, $5, $6)"
            " ON CONFLICT (chat_id, name) DO UPDATE SET criteria = EXCLUDED.criteria,"
            " filters = EXCLUDED.filters, min_score = EXCLUDED.min_score,"
            " notify_enabled = EXCLUDED.notify_enabled, updated_at = now()",
            chat_id,
            name,
            criteria,
            json.dumps(filters or {}, ensure_ascii=False),
            min_score,
            notify_enabled,
        )

    async def set_search_notify(
        self, chat_id: int, name: str, enabled: bool
    ) -> bool:
        """Toggle deal alerts for one saved search; False = search not found.

        Name matching is case-insensitive (same as ``/use`` and the webapp).
        """
        result = await self._require().execute(
            "UPDATE user_searches SET notify_enabled = $3, updated_at = now()"
            " WHERE chat_id = $1 AND LOWER(name) = LOWER($2)",
            chat_id,
            name,
            enabled,
        )
        return result != "UPDATE 0"

    async def delete_search(self, chat_id: int, name: str) -> bool:
        result = await self._require().execute(
            "DELETE FROM user_searches WHERE chat_id = $1 AND name = $2",
            chat_id,
            name,
        )
        return result != "DELETE 0"

    # ---------------------------------------------------------- saved listings

    async def save_listing(
        self,
        chat_id: int,
        ad_id: int,
        title: str | None,
        url: str | None,
        note: str | None = None,
    ) -> None:
        if note is None:
            await self._require().execute(
                "INSERT INTO saved_listings (chat_id, ad_id, ad_title, ad_url)"
                " VALUES ($1, $2, $3, $4) ON CONFLICT (chat_id, ad_id) DO NOTHING",
                chat_id,
                ad_id,
                title,
                url,
            )
            return
        await self._require().execute(
            "INSERT INTO saved_listings (chat_id, ad_id, ad_title, ad_url, note)"
            " VALUES ($1, $2, $3, $4, $5)"
            " ON CONFLICT (chat_id, ad_id) DO UPDATE SET note = EXCLUDED.note",
            chat_id,
            ad_id,
            title,
            url,
            note,
        )

    async def set_saved_note(self, chat_id: int, ad_id: int, note: str) -> bool:
        """Attach a note to a saved ad; False = ad not in this chat's saved list."""
        result = await self._require().execute(
            "UPDATE saved_listings SET note = $3 WHERE chat_id = $1 AND ad_id = $2",
            chat_id,
            ad_id,
            note,
        )
        return result != "UPDATE 0"

    async def set_saved_alert(
        self, chat_id: int, ad_id: int, below: int | None
    ) -> bool:
        """Alert threshold for a saved ad (None = alert on any drop)."""
        result = await self._require().execute(
            "UPDATE saved_listings SET alert_below = $3 WHERE chat_id = $1 AND ad_id = $2",
            chat_id,
            ad_id,
            below,
        )
        return result != "UPDATE 0"

    async def alert_thresholds(self, chat_ids: list[int], ad_id: int) -> dict[int, int | None]:
        """chat_id -> alert_below for savers of one ad (missing key = no row)."""
        if not chat_ids:
            return {}
        rows = await self._require().fetch(
            "SELECT chat_id, alert_below FROM saved_listings"
            " WHERE ad_id = $1 AND chat_id = ANY($2::bigint[])",
            ad_id,
            chat_ids,
        )
        return {r["chat_id"]: r["alert_below"] for r in rows}

    async def list_saved_ids(self, chat_id: int) -> set[int]:
        """Just the saved ad ids (cheap — no listing join, for badge state)."""
        rows = await self._require().fetch(
            "SELECT ad_id FROM saved_listings WHERE chat_id = $1",
            chat_id,
        )
        return {r["ad_id"] for r in rows}

    async def list_saved(self, chat_id: int) -> list[SavedListing]:
        rows = await self._require().fetch(
            "SELECT s.ad_id, s.ad_title, s.ad_url, s.note, s.alert_below,"
            " l.price_eur AS current_price, l.last_seen_at,"
            " (SELECT max(h.price_eur) FROM price_history h WHERE h.ad_id = s.ad_id)"
            " AS max_price"
            " FROM saved_listings s LEFT JOIN listings l ON l.ad_id = s.ad_id"
            " WHERE s.chat_id = $1 ORDER BY s.saved_at DESC",
            chat_id,
        )
        out = []
        for r in rows:
            record = dict(r)
            if record.get("last_seen_at") is not None:
                record["last_seen_at"] = str(record["last_seen_at"])
            out.append(SavedListing(**record))
        return out

    async def remove_saved(self, chat_id: int, ad_id: int) -> bool:
        result = await self._require().execute(
            "DELETE FROM saved_listings WHERE chat_id = $1 AND ad_id = $2",
            chat_id,
            ad_id,
        )
        return result != "DELETE 0"
