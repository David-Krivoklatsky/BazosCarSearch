"""Listing classification: keep cars, drop parts/accessories (Phase 4).

Bazoš ``auto.bazos.sk`` category also contains non-car listings (headlights,
alloy wheels, seats, engines...). The heuristic below uses a keyword blocklist
matched against the title. When the LLM already evaluated a listing, its
``DealEvaluation.is_car`` verdict takes precedence over the heuristic.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from bazcar.core.models import Listing

# Default part/accessory markers (matched case-insensitively as a word PREFIX,
# so "svetl" catches "svetlá"/"svetlo" but not "osvetlenie"). Extend via
# config/scraper.yaml -> markers.part_markers.
#
# Generic intensifiers ("komplet") are deliberately omitted: they identify no
# part on their own (the part noun is always present) and would otherwise drop
# whole cars whose title says "kompletná servisná história".
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
]


def _listed_matches(text: str, markers: Iterable[str]) -> bool:
    """True if any marker begins a word in ``text`` (case-insensitive)."""
    lowered = text.lower()
    for marker in markers:
        needle = marker.strip().lower()
        if needle and re.search(rf"\b{re.escape(needle)}", lowered):
            return True
    return False


def is_car_listing(
    listing: Listing, part_markers: list[str] | None = None
) -> bool:
    """True if the listing looks like a whole car.

    * LLM verdict first (``DealEvaluation.is_car``) — it saw the full text.
    * Otherwise fall back to the keyword heuristic on the TITLE only — car
      descriptions routinely mention wheels/tyres/lights, so matching the
      description would drop almost every real car.
    """
    if listing.evaluation is not None:
        return bool(listing.evaluation.is_car)
    markers = part_markers if part_markers is not None else DEFAULT_PART_MARKERS
    return not _listed_matches(listing.title, markers)


def filter_cars(
    listings: list[Listing], part_markers: list[str] | None = None
) -> list[Listing]:
    """Return only whole-car listings, preserving order."""
    return [
        listing for listing in listings if is_car_listing(listing, part_markers=part_markers)
    ]
