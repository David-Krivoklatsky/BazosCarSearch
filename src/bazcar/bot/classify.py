"""Listing classification: keep cars, drop parts/accessories (Phase 4).

Bazoš ``auto.bazos.sk`` category also contains non-car listings (headlights,
alloy wheels, seats, engines...). The heuristic below uses a keyword blocklist
matched against the title. When the LLM already evaluated a listing, its
``DealEvaluation.is_car`` verdict takes precedence over the heuristic.
"""

from __future__ import annotations

from collections.abc import Iterable

from bazcar.core.models import Listing

# Default part/accessory markers (matched case-insensitively, substring).
# Extend via config/scraper.yaml -> markers.part_markers.
DEFAULT_PART_MARKERS: list[str] = [
    "svetl",  # svetlá
    "zrkadl",  # zrkadlá
    "disky", "ráfik", "koles",  # kolesá
    "pneu",  # pneumatiky
    "naraznik", "nárazník",
    "kapota",
    "prevodovka",
    "sedadl",  # sedačky
    "prístrojovka", "pristrojovka",
    "snímač", "snimac",
    "senzor",
    "tesnenie", "tesneni",
    "elektron",  # elektronika
    "výfuk", "vyfuk",
    "brzd",  # brzdy
    "predný nárazník", "zadný nárazník",
    "komplet", "komplety",
]


def _listed_matches(text: str, markers: Iterable[str]) -> bool:
    lowered = text.lower()
    for marker in markers:
        if marker.lower() in lowered:
            return True
    return False


def is_car_listing(
    listing: Listing, part_markers: list[str] | None = None
) -> bool:
    """True if the listing looks like a whole car.

    * LLM verdict first (``DealEvaluation.is_car``) — it saw the full text.
    * Otherwise fall back to the keyword heuristic (title + description).
    """
    if listing.evaluation is not None:
        return bool(listing.evaluation.is_car)
    markers = part_markers if part_markers is not None else DEFAULT_PART_MARKERS
    text = f"{listing.title} {listing.description or listing.description_preview}"
    return not _listed_matches(text, markers)


def filter_cars(listings: list[Listing]) -> list[Listing]:
    """Return only whole-car listings, preserving order."""
    return [listing for listing in listings if is_car_listing(listing)]
