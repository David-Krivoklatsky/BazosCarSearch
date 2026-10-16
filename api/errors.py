"""Shared JSON response helpers for the Mini App API.

The SPA trusts the ``ok`` flag and the HTTP status, so neither may lie:
``ok`` is False whenever the request could not be completed, and a missing
or unreachable dependency yields 503 instead of a 200 carrying an ``error``
body.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

Response = tuple[dict[str, Any], HTTPStatus]


def ok(**extra: Any) -> Response:
    """A successful response body merged with the given extra fields."""
    payload: dict[str, Any] = {"ok": True}
    payload.update(extra)
    return payload, HTTPStatus.OK


def error(message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> Response:
    """A failed request: ``ok`` is False and the reason is in ``error``."""
    return {"ok": False, "error": message}, status


def unavailable(what: str) -> Response:
    """A dependency (database, saved list, profiles) is not usable."""
    return error(f"{what} unavailable", HTTPStatus.SERVICE_UNAVAILABLE)


def queue_result(status: str, note: str = "", **extra: Any) -> Response:
    """Outcome of a re-score request routed through the cost gate.

    The gate answers ``queued`` / ``awaiting`` / ``nothing`` / ``already``
    (all fine) or ``error`` (the request could not be queued). Only the last
    is a failure, so ``ok`` mirrors that instead of hard-coding True.
    """
    if status == "error":
        payload: dict[str, Any] = {
            "ok": False,
            "error": note or "could not queue the re-score",
            "queued": False,
            "status": status,
        }
        payload.update(extra)
        return payload, HTTPStatus.OK
    return ok(queued=status == "queued", status=status, note=note, **extra)
