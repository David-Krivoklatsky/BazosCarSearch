"""Evaluation profile identity.

Lives in ``bazcar.core`` (import-light, no scraper/LLM deps) so the Vercel
functions (``api/telegram.py``, ``api/webapp.py``) can compute profile keys
without transitively importing ``bs4``/``lxml`` via the scraper stack.
"""

from __future__ import annotations

import hashlib

# Evaluation output schema version — part of the profile key so a prompt
# change never silently reuses evaluations produced by an older contract.
EVAL_SCHEMA_VERSION = "v2"


def profile_key(criteria: str | None, model: str, schema_version: str = EVAL_SCHEMA_VERSION) -> str:
    """Stable key for an evaluation spec (criteria + model + schema) — cache key."""
    return hashlib.md5(f"{criteria or ''}|{model}|{schema_version}".encode()).hexdigest()[:12]
