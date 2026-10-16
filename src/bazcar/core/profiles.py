"""Evaluation profile identity.

Lives in ``bazcar.core`` (import-light, no scraper/LLM deps) so the Vercel
functions (``api/telegram.py``, ``api/webapp.py``) can compute profile keys
without transitively importing ``bs4``/``lxml`` via the scraper stack.

The key identifies WHAT is being judged (criteria + prompt + output schema),
not WHICH model judged it. A car scored once under a search profile keeps
that score when the user switches models; the model that produced it is
recorded on the evaluation row. A forced re-score (explicit user action)
overwrites the row with the current model.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

# Evaluation output schema version — part of the key so a change in the JSON
# contract never silently reuses evaluations produced by an older contract.
EVAL_SCHEMA_VERSION = "v2"


def profile_key(
    criteria: str | None,
    prompt: str = "standard",
    schema_version: str = EVAL_SCHEMA_VERSION,
) -> str:
    """Model-independent cache key for an evaluation spec."""
    base = f"{criteria or ''}|{schema_version}"
    if prompt and prompt != "standard":
        base += f"|{prompt}"
    return hashlib.md5(base.encode()).hexdigest()[:12]


def legacy_profile_key(
    criteria: str | None,
    model: str,
    schema_version: str = EVAL_SCHEMA_VERSION,
    prompt: str = "standard",
) -> str:
    """The pre-v3 key (model included). Used only by the one-time migration."""
    base = f"{criteria or ''}|{model}|{schema_version}"
    if prompt and prompt != "standard":
        base += f"|{prompt}"
    return hashlib.md5(base.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class ActiveProfile:
    """WHAT a user is currently judging (criteria + prompt), plus the active model.

    One object is the single source of truth for the cache key, so a prompt or
    model change can never be forgotten in one call site and applied in another.
    """

    criteria: str | None = None
    prompt: str = "standard"
    model: str | None = None

    @property
    def key(self) -> str:
        return profile_key(self.criteria, prompt=self.prompt)

    @classmethod
    def from_prefs(cls, prefs: Any) -> ActiveProfile:
        """Build from a user-prefs object (``UserPrefs`` or any duck type)."""
        if prefs is None:
            return cls()
        prompt = getattr(prefs, "prompt_name", "standard") or "standard"
        try:
            from bazcar.llm.provider import normalize_prompt_name

            prompt = normalize_prompt_name(prompt)
        except Exception:  # import-light fallback: never fail key derivation
            pass
        return cls(
            criteria=getattr(prefs, "criteria", None),
            prompt=prompt,
            model=getattr(prefs, "model", None),
        )


def legacy_key_mapping(
    criteria_values,
    models,
    prompts=("standard", "bargain", "family"),
    schema_version: str = EVAL_SCHEMA_VERSION,
) -> dict[str, str]:
    """{legacy (model-bound) key: spec key} for every known criteria/model/prompt.

    Keys that do not change (no model involvement) are omitted.
    """
    mapping: dict[str, str] = {}
    for criteria in criteria_values:
        for prompt in prompts:
            new = profile_key(criteria, prompt=prompt, schema_version=schema_version)
            for model in models:
                old = legacy_profile_key(criteria, model, schema_version=schema_version, prompt=prompt)
                if old != new:
                    mapping[old] = new
    return mapping
