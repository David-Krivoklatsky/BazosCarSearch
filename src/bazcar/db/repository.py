"""PostgreSQL repository: schema init, dedupe via upsert, price-change tracking.

Uses ``ad_id`` (the Bazoš ad identifier) as the natural primary key, which makes
repeated scrapes idempotent — the Nth run cannot produce duplicates. Price
history is appended only when a listing is first seen or its price changes, so
``price_history`` is a clean time series per ad.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal

import asyncpg

from bazcar.core.exceptions import DbError
from bazcar.core.models import DealEvaluation, Listing

logger = logging.getLogger(__name__)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS listings (
    ad_id               bigint PRIMARY KEY,
    url                 text NOT NULL,
    title               text NOT NULL,
    price_eur           numeric(12, 2),
    price_raw           text,
    city                text,
    postal_code         text,
    published_date      date,
    views               int,
    description_preview text NOT NULL DEFAULT '',
    description         text,
    image_urls          jsonb NOT NULL DEFAULT '[]'::jsonb,
    year                int,
    mileage_km          bigint,
    source              text NOT NULL DEFAULT 'bazos',
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS price_history (
    id        bigserial PRIMARY KEY,
    ad_id     bigint NOT NULL REFERENCES listings(ad_id) ON DELETE CASCADE,
    price_eur numeric(12, 2),
    seen_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_price_history_ad_id_seen
    ON price_history (ad_id, seen_at);

CREATE TABLE IF NOT EXISTS user_prefs (
    chat_id     bigint PRIMARY KEY,
    criteria    text,
    model       text,
    min_score   int,
    show_photo  boolean NOT NULL DEFAULT false,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_searches (
    id          bigserial PRIMARY KEY,
    chat_id     bigint NOT NULL,
    name        text NOT NULL,
    criteria    text DEFAULT '',
    min_score   int,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (chat_id, name)
);

CREATE TABLE IF NOT EXISTS saved_listings (
    chat_id     bigint NOT NULL,
    ad_id       bigint NOT NULL REFERENCES listings(ad_id) ON DELETE CASCADE,
    ad_title    text,
    ad_url      text,
    saved_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, ad_id)
);

CREATE TABLE IF NOT EXISTS bot_state (
    key         text PRIMARY KEY,
    value       bigint,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- LLM deal scores, one per (listing, search profile) — the same ad is stored
-- once but can carry different scores under different search criteria/models.
CREATE TABLE IF NOT EXISTS evaluations (
    ad_id        bigint NOT NULL REFERENCES listings(ad_id) ON DELETE CASCADE,
    profile_key  text NOT NULL,
    score        int NOT NULL,
    why          text,
    risk         text,
    is_car       boolean NOT NULL DEFAULT true,
    model        text,
    evaluated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ad_id, profile_key)
);

CREATE INDEX IF NOT EXISTS idx_evaluations_profile_score
    ON evaluations (profile_key, score DESC);

-- Backfill tasks: re-evaluate already-stored ads under a (new) search profile.
CREATE TABLE IF NOT EXISTS eval_tasks (
    id           bigserial PRIMARY KEY,
    profile_key  text NOT NULL,
    criteria     text,
    model        text,
    max_listings int NOT NULL DEFAULT 100,
    created_at   timestamptz NOT NULL DEFAULT now(),
    done_at      timestamptz
);

-- Idempotent migrations for tables created before these columns existed.
ALTER TABLE user_prefs
    ADD COLUMN IF NOT EXISTS filters jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE user_searches
    ADD COLUMN IF NOT EXISTS filters jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE user_searches
    ADD COLUMN IF NOT EXISTS min_score int;
"""

_UPSERT_LISTING_SQL = """
INSERT INTO listings (
    ad_id, url, title, price_eur, price_raw, city, postal_code,
    published_date, views, description_preview, description, image_urls,
    year, mileage_km, source, last_seen_at, updated_at
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb,
    $13, $14, $15, now(), now()
)
ON CONFLICT (ad_id) DO UPDATE SET
    url                 = EXCLUDED.url,
    title               = EXCLUDED.title,
    price_eur           = EXCLUDED.price_eur,
    price_raw           = EXCLUDED.price_raw,
    city                = EXCLUDED.city,
    postal_code         = EXCLUDED.postal_code,
    published_date      = EXCLUDED.published_date,
    views               = EXCLUDED.views,
    description_preview = EXCLUDED.description_preview,
    description         = EXCLUDED.description,
    image_urls          = EXCLUDED.image_urls,
    year                = EXCLUDED.year,
    mileage_km          = EXCLUDED.mileage_km,
    last_seen_at        = now(),
    updated_at          = now()
"""

_INSERT_PRICE_HISTORY_SQL = """
INSERT INTO price_history (ad_id, price_eur) VALUES ($1, $2)
"""

_FETCH_EXISTING_SQL = """
SELECT ad_id, price_eur FROM listings WHERE ad_id = ANY($1::bigint[])
"""

_UPSERT_EVALUATION_SQL = """
INSERT INTO evaluations (ad_id, profile_key, score, why, risk, is_car, model)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (ad_id, profile_key) DO UPDATE SET
    score = EXCLUDED.score, why = EXCLUDED.why, risk = EXCLUDED.risk,
    is_car = EXCLUDED.is_car, model = EXCLUDED.model, evaluated_at = now()
"""

_FETCH_EVALUATIONS_SQL = """
SELECT ad_id, score, why, risk, is_car, model
FROM evaluations WHERE profile_key = $1 AND ad_id = ANY($2::bigint[])
"""

_FETCH_RECENT_MATCHES_SQL = """
SELECT l.ad_id, l.url, l.title, l.price_eur, l.city, l.image_urls,
       l.year, l.mileage_km, l.description_preview, l.description,
       e.score, e.why, e.risk, e.model AS eval_model
FROM listings l
JOIN evaluations e ON e.ad_id = l.ad_id AND e.profile_key = $1
WHERE e.score >= $2 AND l.last_seen_at >= now() - make_interval(days => $3)
ORDER BY e.score DESC, l.last_seen_at DESC
LIMIT $4
"""

_FETCH_RECENT_LISTINGS_SQL = """
SELECT ad_id, url, title, price_eur, city, image_urls,
       year, mileage_km, description_preview, description
FROM listings
WHERE last_seen_at >= now() - make_interval(days => $1)
ORDER BY last_seen_at DESC
LIMIT $2
"""


@dataclass
class SyncStats:
    """Result of one scrape -> database sync."""

    total: int = 0
    inserted: int = 0
    price_changed: int = 0
    price_drops: int = 0
    unchanged: int = 0

    @property
    def duplicates_avoided(self) -> int:
        """Rows that already existed and were simply refreshed."""
        return self.unchanged + self.price_changed

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "inserted": self.inserted,
            "price_changed": self.price_changed,
            "price_drops": self.price_drops,
            "unchanged": self.unchanged,
        }


def _listing_record(listing: Listing) -> tuple:
    """Build the parameter tuple for the upsert statement."""
    return (
        listing.ad_id,
        listing.url,
        listing.title,
        listing.price_eur,
        listing.price_raw,
        listing.city,
        listing.postal_code,
        listing.published_date,
        listing.views,
        listing.description_preview,
        listing.description,
        json.dumps(listing.image_urls or [], ensure_ascii=False),
        listing.year,
        listing.mileage_km,
        listing.source,
    )


@dataclass
class SyncPlan:
    """What a sync run will do, computed without touching the database."""

    upserts: list[tuple] = field(default_factory=list)
    history: list[tuple[int, Decimal | None]] = field(default_factory=list)
    stats: SyncStats = field(default_factory=SyncStats)
    inserted_ids: list[int] = field(default_factory=list)


@dataclass
class SyncResult:
    """Dedupe-insert result: aggregate stats plus which ads were inserted."""

    stats: SyncStats
    inserted_ids: list[int]


def plan_sync(listings: list[Listing], existing: dict[int, Decimal | None]) -> SyncPlan:
    """Classify each listing against ``existing`` (ad_id -> stored price).

    Pure function (no I/O) so classification can be unit-tested offline:
      * absent            -> inserted + first price-history row,
      * price changed     -> refreshed + new price-history row,
      * price unchanged   -> refreshed only.
    """
    plan = SyncPlan(stats=SyncStats(total=len(listings)))
    for listing in listings:
        prev_price = existing.get(listing.ad_id)
        plan.upserts.append(_listing_record(listing))
        if prev_price is None:
            plan.stats.inserted += 1
            plan.inserted_ids.append(listing.ad_id)
            plan.history.append((listing.ad_id, listing.price_eur))
        elif prev_price != listing.price_eur:
            plan.stats.price_changed += 1
            if prev_price is not None and listing.price_eur is not None:
                if listing.price_eur < prev_price:
                    plan.stats.price_drops += 1
            plan.history.append((listing.ad_id, listing.price_eur))
        else:
            plan.stats.unchanged += 1
    return plan


class ListingRepository:
    """Thin asyncpg wrapper owning a single connection to the Neon database."""

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

    async def __aenter__(self) -> ListingRepository:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def init_schema(self) -> None:
        """Idempotent schema bootstrap (safe to run on every scrape)."""
        conn = self._require_conn()
        try:
            async with conn.transaction():
                await conn.execute(SCHEMA_SQL)
        except asyncpg.PostgresError as exc:
            raise DbError(f"schema init failed: {exc}") from exc

    async def sync_many(self, listings: list[Listing]) -> SyncResult:
        """Dedupe-insert ``listings`` and append price history rows.

        Classifies each ad against what is already stored (keyed by ``ad_id``):
          * absent   -> inserted + first price-history row,
          * price changed -> refreshed + new price-history row,
          * otherwise -> refreshed only.
        Returns aggregate stats plus the ids of the ads that were newly inserted.
        """
        if not listings:
            return SyncResult(stats=SyncStats(total=0), inserted_ids=[])
        conn = self._require_conn()
        try:
            existing = await self._fetch_existing(conn, [listing.ad_id for listing in listings])
            plan = plan_sync(listings, existing)
            stats = plan.stats
            async with conn.transaction():
                await conn.executemany(_UPSERT_LISTING_SQL, plan.upserts)
                if plan.history:
                    await conn.executemany(_INSERT_PRICE_HISTORY_SQL, plan.history)
            logger.info(
                "db sync: %d total, %d new, %d price_changed, %d unchanged",
                stats.total,
                stats.inserted,
                stats.price_changed,
                stats.unchanged,
            )
            return SyncResult(stats=stats, inserted_ids=plan.inserted_ids)
        except asyncpg.PostgresError as exc:
            raise DbError(f"sync failed: {exc}") from exc

    async def _fetch_existing(
        self, conn: asyncpg.Connection, ad_ids: list[int]
    ) -> dict[int, Decimal | None]:
        if not ad_ids:
            return {}
        rows = await conn.fetch(_FETCH_EXISTING_SQL, ad_ids)
        return {row["ad_id"]: row["price_eur"] for row in rows}

    # ------------------------------------------------------------- evaluations

    async def upsert_evaluations(
        self, profile_key: str, items: list[tuple[int, DealEvaluation]]
    ) -> None:
        """Store LLM scores keyed by (ad_id, profile_key)."""
        if not items:
            return
        conn = self._require_conn()
        try:
            rows = [
                (
                    ad_id,
                    profile_key,
                    ev.score,
                    ev.why,
                    ev.risk,
                    ev.is_car,
                    ev.model,
                )
                for ad_id, ev in items
            ]
            await conn.executemany(_UPSERT_EVALUATION_SQL, rows)
        except asyncpg.PostgresError as exc:
            raise DbError(f"evaluation upsert failed: {exc}") from exc

    async def fetch_evaluations(
        self, profile_key: str, ad_ids: list[int]
    ) -> dict[int, DealEvaluation]:
        """Cached scores for the given profile; missing ads are simply absent."""
        if not ad_ids:
            return {}
        conn = self._require_conn()
        try:
            rows = await conn.fetch(_FETCH_EVALUATIONS_SQL, profile_key, ad_ids)
            return {
                row["ad_id"]: DealEvaluation(
                    score=row["score"],
                    why=row["why"] or "",
                    risk=row["risk"] or "",
                    is_car=row["is_car"],
                    model=row["model"],
                )
                for row in rows
            }
        except asyncpg.PostgresError as exc:
            raise DbError(f"evaluation fetch failed: {exc}") from exc

    async def fetch_recent_matches(
        self, profile_key: str, min_score: int, days: int, limit: int
    ) -> list[dict]:
        """Listings evaluated under ``profile_key`` scoring >= ``min_score``.

        Newest seen first, ``limit`` rows. Used by the bot ``/show`` command.
        """
        conn = self._require_conn()
        try:
            rows = await conn.fetch(
                _FETCH_RECENT_MATCHES_SQL, profile_key, min_score, days, limit
            )
        except asyncpg.PostgresError as exc:
            raise DbError(f"recent matches fetch failed: {exc}") from exc
        matches = []
        for row in rows:
            record = dict(row)
            try:
                record["image_urls"] = json.loads(record["image_urls"])
            except (TypeError, ValueError, json.JSONDecodeError):
                record["image_urls"] = []
            matches.append(record)
        return matches

    async def fetch_evaluations_max_score(self, profile_key: str) -> int | None:
        """Best score achieved under this profile (None = no evaluations yet)."""
        conn = self._require_conn()
        try:
            value = await conn.fetchval(
                "SELECT max(score) FROM evaluations WHERE profile_key = $1", profile_key
            )
            return int(value) if value is not None else None
        except asyncpg.PostgresError as exc:
            raise DbError(f"max score fetch failed: {exc}") from exc

    async def count_evaluations(self, profile_key: str, days: int = 3) -> int:
        """How many ads were scored under this profile (recent window)."""
        conn = self._require_conn()
        try:
            value = await conn.fetchval(
                "SELECT count(*) FROM evaluations e JOIN listings l ON l.ad_id = e.ad_id"
                " WHERE e.profile_key = $1 AND l.last_seen_at >= now() - make_interval(days => $2)",
                profile_key,
                days,
            )
            return int(value or 0)
        except asyncpg.PostgresError as exc:
            raise DbError(f"evaluation count failed: {exc}") from exc

    async def pending_eval_task_for(self, profile_key: str) -> dict | None:
        """Pending backfill task for this profile, if any (bot /status info)."""
        conn = self._require_conn()
        row = await conn.fetchrow(
            "SELECT id, created_at FROM eval_tasks WHERE profile_key = $1 AND done_at IS NULL"
            " ORDER BY id LIMIT 1",
            profile_key,
        )
        return dict(row) if row else None

    async def wipe_listings(self) -> None:
        """Delete every listing (cascades price_history/saved/evaluations)."""
        conn = self._require_conn()
        try:
            await conn.execute(
                "TRUNCATE listings, price_history, saved_listings, evaluations"
            )
        except asyncpg.PostgresError as exc:
            raise DbError(f"wipe failed: {exc}") from exc

    # ------------------------------------------------- evaluation backfill

    async def create_eval_task(
        self, profile_key: str, criteria: str | None, model: str, max_listings: int
    ) -> None:
        """Queue a backfill: score recent ads under this search profile."""
        conn = self._require_conn()
        await conn.execute(
            "INSERT INTO eval_tasks (profile_key, criteria, model, max_listings)"
            " VALUES ($1, $2, $3, $4)",
            profile_key,
            criteria,
            model,
            max_listings,
        )

    async def pending_eval_tasks(self) -> list[dict]:
        conn = self._require_conn()
        rows = await conn.fetch(
            "SELECT id, profile_key, criteria, model, max_listings FROM eval_tasks"
            " WHERE done_at IS NULL ORDER BY id"
        )
        return [dict(row) for row in rows]

    async def complete_eval_task(self, task_id: int) -> None:
        await self._require_conn().execute(
            "UPDATE eval_tasks SET done_at = now() WHERE id = $1", task_id
        )

    async def fetch_recent_listings(self, days: int, limit: int) -> list[dict]:
        """Ads seen within ``days``, any profile — input for eval backfill."""
        conn = self._require_conn()
        try:
            rows = await conn.fetch(_FETCH_RECENT_LISTINGS_SQL, days, limit)
        except asyncpg.PostgresError as exc:
            raise DbError(f"recent listings fetch failed: {exc}") from exc
        listings = []
        for row in rows:
            record = dict(row)
            try:
                record["image_urls"] = json.loads(record["image_urls"])
            except (TypeError, ValueError, json.JSONDecodeError):
                record["image_urls"] = []
            listings.append(record)
        return listings

    def _require_conn(self) -> asyncpg.Connection:
        if self._conn is None:
            raise DbError("repository is not connected; call connect() first")
        return self._conn
