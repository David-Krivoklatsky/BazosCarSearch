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
    chat_id        bigint PRIMARY KEY,
    criteria       text,
    model          text,
    min_score      int,
    show_photo     boolean NOT NULL DEFAULT false,
    notify_enabled boolean NOT NULL DEFAULT true,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_searches (
    id             bigserial PRIMARY KEY,
    chat_id        bigint NOT NULL,
    name           text NOT NULL,
    criteria       text DEFAULT '',
    min_score      int,
    notify_enabled boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (chat_id, name)
);

CREATE TABLE IF NOT EXISTS saved_listings (
    chat_id     bigint NOT NULL,
    ad_id       bigint NOT NULL REFERENCES listings(ad_id) ON DELETE CASCADE,
    ad_title    text,
    ad_url      text,
    note        text NOT NULL DEFAULT '',
    alert_below int,
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
    ad_id              bigint NOT NULL REFERENCES listings(ad_id) ON DELETE CASCADE,
    profile_key        text NOT NULL,
    score              int NOT NULL,
    why                text,
    risk               text,
    is_car             boolean NOT NULL DEFAULT true,
    model              text,
    evaluated_at       timestamptz NOT NULL DEFAULT now(),
    prompt_tokens      int,
    completion_tokens  int,
    cost_usd           numeric(12, 6),
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
    days         int NOT NULL DEFAULT 3,
    attempts     int NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    done_at      timestamptz
);

-- Deal alerts dedupe: each (chat, ad, search profile) is pushed at most once.
CREATE TABLE IF NOT EXISTS notifications (
    chat_id     bigint NOT NULL,
    ad_id       bigint NOT NULL REFERENCES listings(ad_id) ON DELETE CASCADE,
    profile_key text NOT NULL,
    notified_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, ad_id, profile_key)
);

-- Scrape run telemetry: one row per pipeline run, feeds /health + staleness.
CREATE TABLE IF NOT EXISTS scrape_runs (
    id            bigserial PRIMARY KEY,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    pages         int NOT NULL DEFAULT 0,
    total         int NOT NULL DEFAULT 0,
    inserted      int NOT NULL DEFAULT 0,
    price_changed int NOT NULL DEFAULT 0,
    price_drops   int NOT NULL DEFAULT 0,
    evaluated     int NOT NULL DEFAULT 0,
    notified      int NOT NULL DEFAULT 0,
    error         text
);

-- Idempotent migrations for tables created before these columns existed.
ALTER TABLE user_prefs
    ADD COLUMN IF NOT EXISTS filters jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE user_prefs
    ADD COLUMN IF NOT EXISTS show_limit int NOT NULL DEFAULT 5;
ALTER TABLE user_searches
    ADD COLUMN IF NOT EXISTS filters jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE user_searches
    ADD COLUMN IF NOT EXISTS min_score int;
ALTER TABLE eval_tasks
    ADD COLUMN IF NOT EXISTS days int NOT NULL DEFAULT 3;
ALTER TABLE eval_tasks
    ADD COLUMN IF NOT EXISTS attempts int NOT NULL DEFAULT 0;
ALTER TABLE eval_tasks
    ADD COLUMN IF NOT EXISTS total int NOT NULL DEFAULT 0;
ALTER TABLE eval_tasks
    ADD COLUMN IF NOT EXISTS done int NOT NULL DEFAULT 0;
ALTER TABLE eval_tasks
    ADD COLUMN IF NOT EXISTS started_at timestamptz;
ALTER TABLE eval_tasks
    ADD COLUMN IF NOT EXISTS error text;
ALTER TABLE evaluations
    ADD COLUMN IF NOT EXISTS condition text;
ALTER TABLE evaluations
    ADD COLUMN IF NOT EXISTS target_offer int;
ALTER TABLE evaluations
    ADD COLUMN IF NOT EXISTS prompt_tokens int;
ALTER TABLE evaluations
    ADD COLUMN IF NOT EXISTS completion_tokens int;
ALTER TABLE evaluations
    ADD COLUMN IF NOT EXISTS cost_usd numeric(12, 6);
ALTER TABLE saved_listings
    ADD COLUMN IF NOT EXISTS note text NOT NULL DEFAULT '';
ALTER TABLE saved_listings
    ADD COLUMN IF NOT EXISTS alert_below int;
ALTER TABLE user_searches
    ADD COLUMN IF NOT EXISTS notify_enabled boolean NOT NULL DEFAULT true;
ALTER TABLE user_prefs
    ADD COLUMN IF NOT EXISTS notify_enabled boolean NOT NULL DEFAULT true;
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
INSERT INTO evaluations (ad_id, profile_key, score, why, risk, is_car, model,
                         condition, target_offer,
                         prompt_tokens, completion_tokens, cost_usd)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
ON CONFLICT (ad_id, profile_key) DO UPDATE SET
    score = EXCLUDED.score, why = EXCLUDED.why, risk = EXCLUDED.risk,
    is_car = EXCLUDED.is_car, model = EXCLUDED.model, evaluated_at = now(),
    condition = EXCLUDED.condition, target_offer = EXCLUDED.target_offer,
    prompt_tokens = EXCLUDED.prompt_tokens,
    completion_tokens = EXCLUDED.completion_tokens,
    cost_usd = EXCLUDED.cost_usd
"""

_FETCH_EVALUATIONS_SQL = """
SELECT ad_id, score, why, risk, is_car, model, condition, target_offer,
       prompt_tokens, completion_tokens, cost_usd
FROM evaluations WHERE profile_key = $1 AND ad_id = ANY($2::bigint[])
"""

_FETCH_RECENT_MATCHES_SQL = """
SELECT l.ad_id, l.url, l.title, l.price_eur, l.city, l.image_urls,
       l.year, l.mileage_km, l.description_preview, l.description,
       l.published_date, l.first_seen_at,
       e.score, e.why, e.risk, e.model AS eval_model, e.condition, e.target_offer
FROM listings l
JOIN evaluations e ON e.ad_id = l.ad_id AND e.profile_key = $1
WHERE e.score >= $2
  AND COALESCE(l.published_date, l.first_seen_at::date)
      >= (now() - make_interval(days => $3))::date
ORDER BY e.score DESC, l.first_seen_at DESC
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
    # (ad_id, old_price, new_price) for ads whose price went DOWN this sync.
    price_drop_events: list[tuple[int, Decimal, Decimal]] = field(default_factory=list)


@dataclass
class SyncResult:
    """Dedupe-insert result: aggregate stats plus which ads were inserted."""

    stats: SyncStats
    inserted_ids: list[int]
    price_drop_events: list[tuple[int, Decimal, Decimal]] = field(default_factory=list)


def plan_sync(listings: list[Listing], existing: dict[int, Decimal | None]) -> SyncPlan:
    """Classify each listing against ``existing`` (ad_id -> stored price).

    Pure function (no I/O) so classification can be unit-tested offline.
    IMPORTANT: an ``ad_id`` ABSENT from ``existing`` is new; an ``ad_id``
    present with a ``None`` price is a KNOWN ad whose price is unknown —
    these are not the same case:
      * absent            -> inserted + first price-history row,
      * price changed     -> refreshed + new price-history row,
      * price unchanged   -> refreshed only (incl. known-ad/unknown-price).
    """
    plan = SyncPlan(stats=SyncStats(total=len(listings)))
    for listing in listings:
        plan.upserts.append(_listing_record(listing))
        if listing.ad_id not in existing:
            plan.stats.inserted += 1
            plan.inserted_ids.append(listing.ad_id)
            plan.history.append((listing.ad_id, listing.price_eur))
            continue
        prev_price = existing[listing.ad_id]
        if prev_price != listing.price_eur:
            plan.stats.price_changed += 1
            if prev_price is not None and listing.price_eur is not None:
                if listing.price_eur < prev_price:
                    plan.stats.price_drops += 1
                    plan.price_drop_events.append(
                        (listing.ad_id, prev_price, listing.price_eur)
                    )
            plan.history.append((listing.ad_id, listing.price_eur))
        else:
            plan.stats.unchanged += 1
    return plan


class ListingRepository:
    """Thin asyncpg wrapper owning a single connection to the Neon database."""

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
            return SyncResult(
                stats=stats,
                inserted_ids=plan.inserted_ids,
                price_drop_events=plan.price_drop_events,
            )
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
                    ev.condition or None,
                    ev.target_offer,
                    ev.prompt_tokens,
                    ev.completion_tokens,
                    ev.cost_usd,
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
                    condition=row["condition"] or "",
                    target_offer=row["target_offer"],
                    prompt_tokens=row["prompt_tokens"],
                    completion_tokens=row["completion_tokens"],
                    cost_usd=float(row["cost_usd"])
                    if row["cost_usd"] is not None
                    else None,
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

    async def count_recent_matches(
        self, profile_key: str, min_score: int, days: int
    ) -> int:
        """How many listings match (score >= min_score) within the window."""
        conn = self._require_conn()
        try:
            value = await conn.fetchval(
                "SELECT count(*) FROM listings l"
                " JOIN evaluations e ON e.ad_id = l.ad_id AND e.profile_key = $1"
                " WHERE e.score >= $2 AND COALESCE(l.published_date, l.first_seen_at::date)"
                "       >= (now() - make_interval(days => $3))::date",
                profile_key,
                min_score,
                days,
            )
            return int(value or 0)
        except asyncpg.PostgresError as exc:
            raise DbError(f"recent matches count failed: {exc}") from exc

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

    async def count_missing_evaluations(
        self, profile_key: str, days: int, limit: int
    ) -> tuple[int, int]:
        """(missing, total) ads in the window without a score for this profile."""
        rows = await self.fetch_recent_listings(days=days, limit=limit)
        total = len(rows)
        missing = 0
        if not rows:
            return 0, 0
        conn = self._require_conn()
        scored_rows = await conn.fetch(
            "SELECT ad_id FROM evaluations WHERE profile_key = $1"
            " AND ad_id = ANY($2::bigint[])",
            profile_key,
            [row["ad_id"] for row in rows],
        )
        scored = {row["ad_id"] for row in scored_rows}
        missing = sum(1 for row in rows if row["ad_id"] not in scored)
        return missing, total

    # ------------------------------------------------------------ notifications

    async def fetch_notified_ids(
        self, chat_id: int, profile_key: str, ad_ids: list[int]
    ) -> set[int]:
        """Ads already pushed to this chat under this search profile."""
        if not ad_ids:
            return set()
        conn = self._require_conn()
        try:
            rows = await conn.fetch(
                "SELECT ad_id FROM notifications WHERE chat_id = $1 AND profile_key = $2"
                " AND ad_id = ANY($3::bigint[])",
                chat_id,
                profile_key,
                ad_ids,
            )
            return {row["ad_id"] for row in rows}
        except asyncpg.PostgresError as exc:
            raise DbError(f"notifications fetch failed: {exc}") from exc

    async def record_notifications(
        self, chat_id: int, profile_key: str, ad_ids: list[int]
    ) -> None:
        conn = self._require_conn()
        try:
            await conn.executemany(
                "INSERT INTO notifications (chat_id, ad_id, profile_key) VALUES ($1, $2, $3)"
                " ON CONFLICT (chat_id, ad_id, profile_key) DO NOTHING",
                [(chat_id, ad_id, profile_key) for ad_id in ad_ids],
            )
        except asyncpg.PostgresError as exc:
            raise DbError(f"notifications record failed: {exc}") from exc

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
        self, profile_key: str, criteria: str | None, model: str, max_listings: int, days: int = 3
    ) -> None:
        """Queue a backfill: score recent ads under this search profile."""
        conn = self._require_conn()
        await conn.execute(
            "INSERT INTO eval_tasks (profile_key, criteria, model, max_listings, days)"
            " VALUES ($1, $2, $3, $4, $5)",
            profile_key,
            criteria,
            model,
            max_listings,
            days,
        )

    async def bump_eval_task_attempt(self, task_id: int) -> None:
        await self._require_conn().execute(
            "UPDATE eval_tasks SET attempts = attempts + 1 WHERE id = $1", task_id
        )

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

    # ------------------------------------------------------- catalog browsing

    _CATALOG_SORTS = {
        "newest": "l.first_seen_at DESC",
        "price_asc": "l.price_eur ASC NULLS LAST, l.first_seen_at DESC",
        "price_desc": "l.price_eur DESC NULLS LAST, l.first_seen_at DESC",
        "score": "e.score DESC NULLS LAST, l.first_seen_at DESC",
    }
    _CATALOG_STATUS = {
        "all": "TRUE",
        "unevaluated": "e.ad_id IS NULL",
        "rated": "e.ad_id IS NOT NULL",
    }

    @staticmethod
    def _catalog_filters(
        q: str | None,
        min_price: int | None,
        max_price: int | None,
        min_year: int | None,
        max_year: int | None,
        start: int = 3,
    ) -> tuple[str, list]:
        """Extra AND-conditions for catalog browsing (all parameterized)."""
        conds: list[str] = []
        params: list = []
        idx = start
        if q:
            conds.append(f"(l.title ILIKE ${idx} OR l.city ILIKE ${idx})")
            params.append(f"%{q.strip()}%")
            idx += 1
        if min_price is not None:
            conds.append(f"l.price_eur >= ${idx}")
            params.append(min_price)
            idx += 1
        if max_price is not None:
            conds.append(f"l.price_eur <= ${idx}")
            params.append(max_price)
            idx += 1
        if min_year is not None:
            conds.append(f"l.year >= ${idx}")
            params.append(min_year)
            idx += 1
        if max_year is not None:
            conds.append(f"l.year <= ${idx}")
            params.append(max_year)
            idx += 1
        return (" AND " + " AND ".join(conds)) if conds else "", params

    async def fetch_catalog(
        self,
        profile_key: str,
        days: int,
        limit: int,
        offset: int = 0,
        sort: str = "newest",
        status: str = "all",
        q: str | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        min_year: int | None = None,
        max_year: int | None = None,
    ) -> list[dict]:
        """Every stored listing in the window, evaluated or not.

        The evaluation (when present) is the active profile's — listings that
        were never scored simply have NULL score fields. ``sort`` and
        ``status`` are whitelisted keys, never raw user input.
        """
        order = self._CATALOG_SORTS.get(sort, self._CATALOG_SORTS["newest"])
        cond = self._CATALOG_STATUS.get(status, "TRUE")
        extra, params = self._catalog_filters(
            q, min_price, max_price, min_year, max_year, start=5
        )
        sql = f"""
        SELECT l.ad_id, l.url, l.title, l.price_eur, l.city, l.image_urls,
               l.year, l.mileage_km, l.description_preview,
               l.published_date, l.first_seen_at, l.last_seen_at,
               e.score, e.why, e.risk, e.is_car, e.model AS eval_model,
               e.condition, e.target_offer
        FROM listings l
        LEFT JOIN evaluations e ON e.ad_id = l.ad_id AND e.profile_key = $1
        WHERE COALESCE(l.published_date, l.first_seen_at::date)
              >= (now() - make_interval(days => $2))::date
          AND {cond}{extra}
        ORDER BY {order}
        LIMIT $3 OFFSET $4
        """  # noqa: S608 — order/cond come from whitelists above
        conn = self._require_conn()
        try:
            rows = await conn.fetch(sql, profile_key, days, limit, offset, *params)
        except asyncpg.PostgresError as exc:
            raise DbError(f"catalog fetch failed: {exc}") from exc
        return [self._decode_catalog_row(r) for r in rows]

    async def count_catalog(
        self,
        profile_key: str,
        days: int,
        status: str = "all",
        q: str | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        min_year: int | None = None,
        max_year: int | None = None,
    ) -> int:
        cond = self._CATALOG_STATUS.get(status, "TRUE")
        extra, params = self._catalog_filters(
            q, min_price, max_price, min_year, max_year, start=3
        )
        sql = f"""
        SELECT count(*) FROM listings l
        LEFT JOIN evaluations e ON e.ad_id = l.ad_id AND e.profile_key = $1
        WHERE COALESCE(l.published_date, l.first_seen_at::date)
              >= (now() - make_interval(days => $2))::date
          AND {cond}{extra}
        """  # noqa: S608 — cond comes from the whitelist above
        conn = self._require_conn()
        try:
            value = await conn.fetchval(sql, profile_key, days, *params)
            return int(value or 0)
        except asyncpg.PostgresError as exc:
            raise DbError(f"catalog count failed: {exc}") from exc

    async def fetch_listing_by_id(self, ad_id: int, profile_key: str) -> dict | None:
        """One listing plus the active profile's evaluation, when present."""
        conn = self._require_conn()
        try:
            row = await conn.fetchrow(
                """
                SELECT l.ad_id, l.url, l.title, l.price_eur, l.city, l.image_urls,
                       l.year, l.mileage_km, l.description_preview, l.description,
                       l.published_date, l.first_seen_at, l.last_seen_at, l.views,
                       e.score, e.why, e.risk, e.is_car, e.model AS eval_model,
                       e.condition, e.target_offer
                FROM listings l
                LEFT JOIN evaluations e ON e.ad_id = l.ad_id AND e.profile_key = $1
                WHERE l.ad_id = $2
                """,
                profile_key,
                ad_id,
            )
        except asyncpg.PostgresError as exc:
            raise DbError(f"listing fetch failed: {exc}") from exc
        return self._decode_catalog_row(row) if row else None

    @staticmethod
    def _decode_catalog_row(row) -> dict:
        record = dict(row)
        try:
            record["image_urls"] = json.loads(record["image_urls"])
        except (TypeError, ValueError, json.JSONDecodeError):
            record["image_urls"] = []
        return record

    async def fetch_price_history(self, ad_id: int, limit: int = 10) -> list[dict]:
        """Chronological price points for one ad (oldest first)."""
        conn = self._require_conn()
        try:
            rows = await conn.fetch(
                "SELECT price_eur, seen_at FROM ("
                "  SELECT price_eur, seen_at FROM price_history"
                "  WHERE ad_id = $1 ORDER BY id DESC LIMIT $2"
                ") t ORDER BY seen_at, t.price_eur",
                ad_id,
                limit,
            )
            return [dict(r) for r in rows]
        except asyncpg.PostgresError as exc:
            raise DbError(f"price history fetch failed: {exc}") from exc

    # ------------------------------------------------------------- statistics

    async def stats_overview(self, profile_key: str) -> dict:
        """Aggregate numbers for /stats and the dashboard overview."""
        conn = self._require_conn()
        try:
            total = int(await conn.fetchval("SELECT count(*) FROM listings") or 0)
            evaluated = int(
                await conn.fetchval(
                    "SELECT count(*) FROM evaluations WHERE profile_key = $1", profile_key
                )
                or 0
            )
            price = await conn.fetchrow(
                "SELECT min(price_eur) AS mn, max(price_eur) AS mx,"
                " avg(price_eur)::int AS avg,"
                " percentile_cont(0.5) WITHIN GROUP (ORDER BY price_eur) AS median"
                " FROM listings WHERE price_eur IS NOT NULL"
            )
            bands = await conn.fetchrow(
                "SELECT"
                " count(*) FILTER (WHERE score >= 90) AS b90,"
                " count(*) FILTER (WHERE score >= 80 AND score < 90) AS b80,"
                " count(*) FILTER (WHERE score >= 70 AND score < 80) AS b70,"
                " count(*) FILTER (WHERE score >= 50 AND score < 70) AS b50,"
                " count(*) FILTER (WHERE score < 50) AS b0"
                " FROM evaluations WHERE profile_key = $1",
                profile_key,
            )
            new_by_day = await conn.fetch(
                "SELECT d::date AS day, count(*) AS n FROM ("
                "  SELECT COALESCE(published_date, first_seen_at::date) AS d FROM listings"
                ") t WHERE d >= (now() - make_interval(days => 7))::date"
                " GROUP BY 1 ORDER BY 1"
            )
            drops_7d = int(
                await conn.fetchval(
                    "SELECT count(*) FROM ("
                    "  SELECT ad_id, price_eur,"
                    "    lag(price_eur) OVER (PARTITION BY ad_id ORDER BY id) AS prev,"
                    "    seen_at FROM price_history"
                    ") t WHERE prev IS NOT NULL AND price_eur IS NOT NULL"
                    "  AND price_eur < prev"
                    "  AND seen_at >= now() - make_interval(days => 7)"
                )
                or 0
            )
            return {
                "total": total,
                "evaluated": evaluated,
                "price": dict(price) if price else {},
                "bands": {k: int(v) for k, v in dict(bands).items()} if bands else {},
                "new_by_day": [(str(r["day"]), int(r["n"])) for r in new_by_day],
                "drops_7d": drops_7d,
            }
        except asyncpg.PostgresError as exc:
            raise DbError(f"stats overview failed: {exc}") from exc

    _BENCHMARK_SQL = """
    WITH base AS (
        SELECT price_eur FROM listings
        WHERE price_eur IS NOT NULL AND title ILIKE $1
          AND ($2::int IS NULL OR year BETWEEN $2 - 2 AND $2 + 2)
          AND ($3::text IS NULL OR title ILIKE '%' || $3 || '%')
          AND ($4::bigint IS NULL OR mileage_km IS NULL
               OR ABS(mileage_km - $4) <= 50000)
    ),
    bounds AS (
        SELECT percentile_cont(0.05) WITHIN GROUP (ORDER BY price_eur) AS lo,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY price_eur) AS hi,
               count(*) AS n FROM base
    ),
    kept AS (
        SELECT b.price_eur FROM base b, bounds
        WHERE bounds.n < 10 OR (b.price_eur BETWEEN bounds.lo AND bounds.hi)
    )
    SELECT (SELECT n FROM bounds) AS n,
           count(*) AS kept,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY price_eur) AS median,
           percentile_cont(0.1) WITHIN GROUP (ORDER BY price_eur) AS p10,
           percentile_cont(0.25) WITHIN GROUP (ORDER BY price_eur) AS p25,
           percentile_cont(0.75) WITHIN GROUP (ORDER BY price_eur) AS p75,
           percentile_cont(0.9) WITHIN GROUP (ORDER BY price_eur) AS p90,
           min(price_eur) AS mn, max(price_eur) AS mx
    FROM kept
    """

    async def price_benchmark(
        self,
        make: str,
        year: int | None,
        model: str | None = None,
        mileage_km: int | None = None,
    ) -> dict | None:
        """Asking-price benchmark for comparable listings (same make, ±2 years).

        Trims p5/p95 outliers once the sample has >= 10 rows, optionally
        narrows to the same model token and a ±50k km mileage band (falling
        back to the wider sample when the narrow one has < 5 rows).

        This is a distribution of OBSERVED asking prices on Bazoš, not a
        verified market value — the caller must show the sample size.
        """
        conn = self._require_conn()
        try:
            row = await conn.fetchrow(
                self._BENCHMARK_SQL, f"{make}%", year, model, mileage_km
            )
            if row is None or not row["n"]:
                return None
            if mileage_km is not None and (row["kept"] or 0) < 5:
                row = await conn.fetchrow(
                    self._BENCHMARK_SQL, f"{make}%", year, model, None
                )
                if row is None or not row["n"]:
                    return None
            result = dict(row)
            result["trimmed"] = bool(result["n"] and result["n"] >= 10)
            return result
        except asyncpg.PostgresError as exc:
            raise DbError(f"price benchmark failed: {exc}") from exc

    # --------------------------------------------------- extended statistics

    async def price_histogram(self, buckets: int = 8) -> list[dict]:
        """Price distribution buckets across the whole catalog."""
        buckets = max(2, min(int(buckets or 8), 20))
        conn = self._require_conn()
        try:
            rows = await conn.fetch(
                "WITH b AS (SELECT min(price_eur) AS mn, max(price_eur) AS mx"
                " FROM listings WHERE price_eur IS NOT NULL)"
                " SELECT width_bucket(price_eur, b.mn, b.mx, $1) AS bucket,"
                " count(*) AS n, min(price_eur) AS lo, max(price_eur) AS hi"
                " FROM listings, b WHERE price_eur IS NOT NULL"
                " GROUP BY 1 ORDER BY 1",
                buckets,
            )
            return [dict(r) for r in rows]
        except asyncpg.PostgresError as exc:
            raise DbError(f"price histogram failed: {exc}") from exc

    _MAKE_STOPLIST = (
        "'PREDÁM','PREDAM','PREDAJ','PONÚKAM','PONUKAM','AUTO','AUTÁ','AUTA',"
        "'NOVÉ','NOVE','TOP','SUPER','LACNO','DIESEL','BENZÍN','BENZIN','NA','ZA'"
    )

    async def top_makes(self, limit: int = 10) -> list[dict]:
        """Most common makes (first title token, case-folded) with median price."""
        conn = self._require_conn()
        try:
            rows = await conn.fetch(
                "SELECT upper(split_part(title, ' ', 1)) AS make, count(*) AS n,"
                " percentile_cont(0.5) WITHIN GROUP (ORDER BY price_eur) AS median"
                " FROM listings"
                f" WHERE upper(split_part(title, ' ', 1)) NOT IN ({self._MAKE_STOPLIST})"
                " GROUP BY 1 ORDER BY n DESC LIMIT $1",
                max(1, min(int(limit or 10), 30)),
            )
            return [dict(r) for r in rows]
        except asyncpg.PostgresError as exc:
            raise DbError(f"top makes failed: {exc}") from exc

    async def score_vs_price(self, profile_key: str) -> list[dict]:
        """Average asking price per score band (is this profile's taste cheaper?)."""
        conn = self._require_conn()
        try:
            rows = await conn.fetch(
                "SELECT CASE WHEN score >= 90 THEN '90+'"
                " WHEN score >= 80 THEN '80-89' WHEN score >= 70 THEN '70-79'"
                " WHEN score >= 50 THEN '50-69' ELSE '<50' END AS band,"
                " count(*) AS n, avg(price_eur)::int AS avg_price,"
                " percentile_cont(0.5) WITHIN GROUP (ORDER BY price_eur) AS median_price"
                " FROM evaluations e LEFT JOIN listings l ON l.ad_id = e.ad_id"
                " WHERE e.profile_key = $1 GROUP BY 1",
                profile_key,
            )
            order = {"90+": 0, "80-89": 1, "70-79": 2, "50-69": 3, "<50": 4}
            return sorted(
                [dict(r) for r in rows], key=lambda r: order.get(r["band"], 9)
            )
        except asyncpg.PostgresError as exc:
            raise DbError(f"score vs price failed: {exc}") from exc

    async def new_by_day(self, days: int = 30) -> list[tuple[str, int]]:
        """Daily new-listing counts for the last ``days`` days."""
        days = max(1, min(int(days or 30), 365))
        conn = self._require_conn()
        try:
            rows = await conn.fetch(
                "SELECT d::date AS day, count(*) AS n FROM ("
                "  SELECT COALESCE(published_date, first_seen_at::date) AS d FROM listings"
                ") t WHERE d >= (now() - make_interval(days => $1))::date"
                " GROUP BY 1 ORDER BY 1",
                days,
            )
            return [(str(r["day"]), int(r["n"])) for r in rows]
        except asyncpg.PostgresError as exc:
            raise DbError(f"new-by-day failed: {exc}") from exc

    async def coverage_trend(
        self, profile_key: str, days: int = 14
    ) -> list[tuple[str, int]]:
        """Evaluations created per day (AI coverage momentum)."""
        days = max(1, min(int(days or 14), 90))
        conn = self._require_conn()
        try:
            rows = await conn.fetch(
                "SELECT evaluated_at::date AS day, count(*) AS n FROM evaluations"
                " WHERE profile_key = $1 AND evaluated_at >= now() - make_interval(days => $2)"
                " GROUP BY 1 ORDER BY 1",
                profile_key,
                days,
            )
            return [(str(r["day"]), int(r["n"])) for r in rows]
        except asyncpg.PostgresError as exc:
            raise DbError(f"coverage trend failed: {exc}") from exc

    async def drops_leaderboard(self, limit: int = 10, days: int = 30) -> list[dict]:
        """Biggest % price drops with listing context (is_car-aware via evals)."""
        conn = self._require_conn()
        try:
            rows = await conn.fetch(
                "SELECT t.ad_id, l.title, l.url, l.city, t.prev, t.price_eur AS now_price,"
                " round((1 - t.price_eur / NULLIF(t.prev, 0)) * 100, 1) AS pct"
                " FROM (SELECT ad_id, price_eur,"
                "  lag(price_eur) OVER (PARTITION BY ad_id ORDER BY id) AS prev,"
                "  seen_at FROM price_history) t"
                " JOIN listings l ON l.ad_id = t.ad_id"
                " WHERE t.prev IS NOT NULL AND t.price_eur IS NOT NULL"
                "  AND t.price_eur < t.prev AND t.prev > 0"
                "  AND t.seen_at >= now() - make_interval(days => $2)"
                " ORDER BY pct DESC LIMIT $1",
                max(1, min(int(limit or 10), 30)),
                max(1, min(int(days or 30), 365)),
            )
            return [dict(r) for r in rows]
        except asyncpg.PostgresError as exc:
            raise DbError(f"drops leaderboard failed: {exc}") from exc

    async def funnel(
        self, profile_key: str, chat_id: int | None = None
    ) -> dict[str, int]:
        """Pipeline funnel: scraped -> evaluated -> notified -> saved."""
        conn = self._require_conn()
        try:
            total = int(await conn.fetchval("SELECT count(*) FROM listings") or 0)
            evaluated = int(
                await conn.fetchval(
                    "SELECT count(*) FROM evaluations WHERE profile_key = $1",
                    profile_key,
                )
                or 0
            )
            if chat_id is None:
                notified = int(
                    await conn.fetchval(
                        "SELECT count(*) FROM notifications WHERE profile_key = $1",
                        profile_key,
                    )
                    or 0
                )
                saved = int(
                    await conn.fetchval("SELECT count(*) FROM saved_listings") or 0
                )
            else:
                notified = int(
                    await conn.fetchval(
                        "SELECT count(*) FROM notifications"
                        " WHERE profile_key = $1 AND chat_id = $2",
                        profile_key,
                        chat_id,
                    )
                    or 0
                )
                saved = int(
                    await conn.fetchval(
                        "SELECT count(*) FROM saved_listings WHERE chat_id = $1",
                        chat_id,
                    )
                    or 0
                )
            return {
                "scraped": total,
                "evaluated": evaluated,
                "notified": notified,
                "saved": saved,
            }
        except asyncpg.PostgresError as exc:
            raise DbError(f"funnel failed: {exc}") from exc

    async def eval_cost(
        self, profile_key: str | None = None, days: int = 30
    ) -> dict:
        """LLM spend: totals + per-model breakdown (NULL usage = unknown)."""
        days = max(1, min(int(days or 30), 365))
        conn = self._require_conn()
        try:
            cond = ""
            params: list = []
            if profile_key:
                cond = "AND profile_key = $2"
                params = [profile_key]
            total = await conn.fetchrow(
                "SELECT count(*) AS n,"
                " coalesce(sum(prompt_tokens), 0) AS prompt_tokens,"
                " coalesce(sum(completion_tokens), 0) AS completion_tokens,"
                " coalesce(sum(cost_usd), 0) AS cost_usd,"
                " count(*) FILTER (WHERE prompt_tokens IS NULL) AS unknown"
                f" FROM evaluations WHERE evaluated_at >= now() - make_interval(days => $1) {cond}",
                days,
                *params,
            )
            by_model = await conn.fetch(
                "SELECT coalesce(model, '?') AS model, count(*) AS n,"
                " coalesce(sum(prompt_tokens), 0) AS prompt_tokens,"
                " coalesce(sum(completion_tokens), 0) AS completion_tokens,"
                " coalesce(sum(cost_usd), 0) AS cost_usd"
                " FROM evaluations"
                f" WHERE evaluated_at >= now() - make_interval(days => $1) {cond}"
                " GROUP BY 1 ORDER BY n DESC",
                days,
                *params,
            )
            return {"total": dict(total), "by_model": [dict(r) for r in by_model]}
        except asyncpg.PostgresError as exc:
            raise DbError(f"eval cost failed: {exc}") from exc

    # --------------------------------------------------------- price alerts

    async def fetch_chats_saving(self, ad_ids: list[int]) -> dict[int, list[int]]:
        """ad_id -> [chat_id, ...] for users who saved any of these ads."""
        if not ad_ids:
            return {}
        conn = self._require_conn()
        try:
            rows = await conn.fetch(
                "SELECT chat_id, ad_id FROM saved_listings WHERE ad_id = ANY($1::bigint[])",
                ad_ids,
            )
            result: dict[int, list[int]] = {}
            for row in rows:
                result.setdefault(row["ad_id"], []).append(row["chat_id"])
            return result
        except asyncpg.PostgresError as exc:
            raise DbError(f"saved-by fetch failed: {exc}") from exc

    # ------------------------------------------------------------ scrape runs

    async def record_scrape_run(
        self,
        *,
        pages: int,
        total: int,
        inserted: int,
        price_changed: int,
        price_drops: int,
        evaluated: int,
        notified: int,
        error: str | None = None,
    ) -> None:
        conn = self._require_conn()
        try:
            await conn.execute(
                "INSERT INTO scrape_runs (pages, total, inserted, price_changed,"
                " price_drops, evaluated, notified, error, finished_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())",
                pages,
                total,
                inserted,
                price_changed,
                price_drops,
                evaluated,
                notified,
                error,
            )
        except asyncpg.PostgresError as exc:
            raise DbError(f"scrape run record failed: {exc}") from exc

    async def latest_scrape_run(self) -> dict | None:
        conn = self._require_conn()
        try:
            row = await conn.fetchrow(
                "SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 1"
            )
            return dict(row) if row else None
        except asyncpg.PostgresError as exc:
            raise DbError(f"latest scrape run failed: {exc}") from exc

    # ------------------------------------------------- eval task progress

    async def start_eval_task(self, task_id: int, total: int) -> None:
        await self._require_conn().execute(
            "UPDATE eval_tasks SET started_at = now(), total = $2, done = 0 WHERE id = $1",
            task_id,
            total,
        )

    async def set_eval_task_progress(self, task_id: int, done: int) -> None:
        await self._require_conn().execute(
            "UPDATE eval_tasks SET done = $2 WHERE id = $1", task_id, done
        )

    async def fail_eval_task(self, task_id: int, error: str) -> None:
        """Abandon a task: mark it finished with an explanatory error."""
        await self._require_conn().execute(
            "UPDATE eval_tasks SET done_at = now(), error = $2 WHERE id = $1",
            task_id,
            error,
        )

    async def pending_eval_tasks(self) -> list[dict]:
        conn = self._require_conn()
        rows = await conn.fetch(
            "SELECT id, profile_key, criteria, model, max_listings, days, attempts,"
            " total, done, started_at, created_at FROM eval_tasks"
            " WHERE done_at IS NULL ORDER BY id"
        )
        return [dict(row) for row in rows]

    async def recent_finished_eval_tasks(self, limit: int = 3) -> list[dict]:
        conn = self._require_conn()
        rows = await conn.fetch(
            "SELECT id, profile_key, criteria, model, total, done, error, done_at"
            " FROM eval_tasks WHERE done_at IS NOT NULL ORDER BY done_at DESC LIMIT $1",
            limit,
        )
        return [dict(row) for row in rows]

    def _require_conn(self) -> asyncpg.Connection:
        if self._conn is None:
            raise DbError("repository is not connected; call connect() first")
        return self._conn
