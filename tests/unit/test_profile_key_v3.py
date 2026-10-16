"""v3 score-key model: model-independent keys and the legacy migration."""

from __future__ import annotations

import asyncio

from bazcar.core.profiles import (
    EVAL_SCHEMA_VERSION,
    legacy_key_mapping,
    legacy_profile_key,
    profile_key,
)


def test_key_ignores_model_by_construction() -> None:
    # The model is not an input at all: switching models reuses the same key.
    assert profile_key("diesel") == profile_key("diesel", prompt="standard")
    assert legacy_profile_key("diesel", "m/1") != legacy_profile_key("diesel", "m/2")


def test_prompt_and_schema_still_fork_the_key() -> None:
    assert profile_key("c") != profile_key("c", prompt="bargain")
    assert profile_key("c", prompt="bargain") != profile_key("c", prompt="family")
    assert profile_key("c") != profile_key("c", schema_version="v1")


def test_standard_prompt_legacy_key_is_reproducible() -> None:
    # the legacy (model-bound) key for 'standard' is exactly the historical formula
    import hashlib

    expected = hashlib.md5(f"c|m/1|{EVAL_SCHEMA_VERSION}".encode()).hexdigest()[:12]
    assert legacy_profile_key("c", "m/1") == expected


def test_mapping_covers_every_model_and_prompt() -> None:
    mapping = legacy_key_mapping(["diesel"], ["m/1", "m/2"])
    # 2 models x 3 prompts, all different from the spec key
    assert len(mapping) == 6
    assert set(mapping.values()) == {
        profile_key("diesel", prompt=p) for p in ("standard", "bargain", "family")
    }
    assert legacy_profile_key("diesel", "m/1") in mapping


def test_mapping_is_empty_without_inputs() -> None:
    assert legacy_key_mapping([], ["m/1"]) == {}
    assert legacy_key_mapping(["diesel"], []) == {}


def test_active_profile_threads_prompt_not_just_criteria() -> None:
    from types import SimpleNamespace

    from bazcar.core.profiles import ActiveProfile

    prefs = SimpleNamespace(criteria="diesel", prompt_name="bargain", model="m/1")
    ap = ActiveProfile.from_prefs(prefs)
    assert ap.key == profile_key("diesel", prompt="bargain")
    assert ap.key != profile_key("diesel")  # the prompt-blind bug is fixed
    assert ap.model == "m/1"
    assert ActiveProfile.from_prefs(None).key == profile_key(None)


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _MigrationConn:
    """Records the statements the migration issues; ``legacy_left`` toggles idempotence."""

    def __init__(self, legacy_left: bool):
        self.legacy_left = legacy_left
        self.statements: list[str] = []

    def transaction(self):
        return _Tx()

    async def fetchval(self, sql, *args):
        self.statements.append(sql)
        return 1 if self.legacy_left else None

    async def execute(self, sql, *args):
        self.statements.append(sql)
        return "DELETE 2" if sql.lstrip().startswith("DELETE") else "UPDATE 1"


def _repo(conn):
    from bazcar.db import ListingRepository

    repo = ListingRepository("postgres://fake")
    repo._conn = conn
    return repo


def test_migration_rewrites_all_three_tables_and_dedupes_first() -> None:
    conn = _MigrationConn(legacy_left=True)
    mapping = {"old1": "new1", "old2": "new1"}
    counts = asyncio.run(_repo(conn).migrate_profile_keys(mapping))
    sql = conn.statements
    # the existence check comes first, then per table: dedupe (DELETE) before rename (UPDATE)
    assert sql[0].startswith("SELECT 1 FROM evaluations")
    assert sql[1].startswith("DELETE FROM evaluations")
    assert sql[2].startswith("UPDATE evaluations") and "unnest" in sql[2]
    assert sql.index(next(s for s in sql if s.startswith("DELETE FROM notifications"))) < \
        sql.index(next(s for s in sql if s.startswith("UPDATE notifications")))
    assert any(s.startswith("UPDATE eval_tasks") and "unnest" in s for s in sql)
    # one statement per table regardless of how many keys are mapped
    assert sum(1 for s in sql if s.startswith("UPDATE")) == 3
    assert counts["evaluations"] == 1 and counts["notifications"] == 1 and counts["eval_tasks"] == 1


def test_migration_scopes_dedupe_to_each_target_group() -> None:
    # Two legacy keys collapsing into DIFFERENT spec keys are different profiles:
    # dedupe must run once per target group, never across them, or a valid score
    # for the other profile would be deleted.
    conn = _MigrationConn(legacy_left=True)
    counts = asyncio.run(_repo(conn).migrate_profile_keys({"a": "x", "b": "y"}))
    deletes = [s for s in conn.statements if s.startswith("DELETE FROM evaluations")]
    assert len(deletes) == 2
    assert sum(1 for s in conn.statements if s.startswith("UPDATE")) == 3
    assert counts["evaluations"] == 1


def test_migration_is_a_noop_once_legacy_keys_are_gone() -> None:
    conn = _MigrationConn(legacy_left=False)
    counts = asyncio.run(_repo(conn).migrate_profile_keys({"old1": "new1"}))
    assert counts == {"evaluations": 0, "notifications": 0, "eval_tasks": 0}
    assert len(conn.statements) == 1  # only the existence check


def test_migration_with_empty_mapping_touches_nothing() -> None:
    conn = _MigrationConn(legacy_left=True)
    assert asyncio.run(_repo(conn).migrate_profile_keys({})) == {
        "evaluations": 0, "notifications": 0, "eval_tasks": 0,
    }
    assert conn.statements == []
