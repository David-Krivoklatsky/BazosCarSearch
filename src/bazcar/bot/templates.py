"""Search-profile templates for the bot (/templates).

Each template is a ready-made starting point: criteria + Bazoš filters + a
suggested minimum score. Applying one (inline button or ``/use``) stores it as
a regular search profile, so the user can tweak criteria and filters freely
afterwards. Keep templates honest — they configure the pipeline, nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SearchTemplate:
    key: str
    emoji: str
    name: str
    description: str
    criteria: str
    filters: dict = field(default_factory=dict)
    min_score: int = 60


TEMPLATES: list[SearchTemplate] = [
    SearchTemplate(
        key="rodinne",
        emoji="👨‍👩‍👧",
        name="Family",
        description="wagon/SUV, safe, under 150,000 km, €2,000–8,000",
        criteria="family car, wagon or SUV, safe, spacious, under 150,000 km",
        filters={"min_price": 2000, "max_price": 8000},
        min_score=65,
    ),
    SearchTemplate(
        key="mesto",
        emoji="🏙️",
        name="City",
        description="small car, low fuel use, under 120,000 km, €1,000–5,000",
        criteria="small city car, low fuel consumption, easy maintenance, under 120,000 km",
        filters={"min_price": 1000, "max_price": 5000},
        min_score=60,
    ),
    SearchTemplate(
        key="dialnica",
        emoji="🛣️",
        name="Highway",
        description="diesel, under 200,000 km, over 80 kW, €3,000–12,000",
        criteria="diesel for long trips and highway, under 200,000 km, over 80 kW, cruise control",
        filters={"min_price": 3000, "max_price": 12000},
        min_score=65,
    ),
    SearchTemplate(
        key="prve",
        emoji="🎓",
        name="First car",
        description="cheap, reliable, simple, €800–3,000",
        criteria="first car for a beginner, cheap, reliable, cheap and easy maintenance",
        filters={"min_price": 800, "max_price": 3000},
        min_score=55,
    ),
    SearchTemplate(
        key="prilezitost",
        emoji="💎",
        name="Bargain",
        description="well under market, quick sale, no price filter",
        criteria="bargain well under market price, good condition for little money, quick sale",
        filters={},
        min_score=75,
    ),
]


def get_template(key: str) -> SearchTemplate | None:
    for template in TEMPLATES:
        if template.key == key:
            return template
    return None
