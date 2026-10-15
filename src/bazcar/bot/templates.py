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
        name="Rodinné",
        description="kombi/SUV, bezpečné, do 150 000 km, 2 000–8 000 €",
        criteria="rodinné auto, kombi alebo SUV, bezpečné, priestranné, do 150 000 km",
        filters={"min_price": 2000, "max_price": 8000},
        min_score=65,
    ),
    SearchTemplate(
        key="mesto",
        emoji="🏙️",
        name="Do mesta",
        description="malé auto, nízka spotreba, do 120 000 km, 1 000–5 000 €",
        criteria="malé auto do mesta, nízka spotreba, jednoduchá údržba, do 120 000 km",
        filters={"min_price": 1000, "max_price": 5000},
        min_score=60,
    ),
    SearchTemplate(
        key="dialnica",
        emoji="🛣️",
        name="Na diaľnicu",
        description="diesel, do 200 000 km, nad 80 kW, 3 000–12 000 €",
        criteria="diesel na dlhé trate a diaľnicu, do 200 000 km, výkon nad 80 kW, tempomat",
        filters={"min_price": 3000, "max_price": 12000},
        min_score=65,
    ),
    SearchTemplate(
        key="prve",
        emoji="🎓",
        name="Prvé auto",
        description="lacné, spoľahlivé, jednoduché, 800–3 000 €",
        criteria="prvé auto pre začiatočníka, lacné, spoľahlivé, jednoduchá a lacná údržba",
        filters={"min_price": 800, "max_price": 3000},
        min_score=55,
    ),
    SearchTemplate(
        key="prilezitost",
        emoji="💎",
        name="Príležitosť",
        description="výrazne pod trhom, rýchly predaj, bez cenového filtra",
        criteria="výhodná kúpa výrazne pod trhovou cenou, dobrý stav za nízku cenu, rýchly predaj",
        filters={},
        min_score=75,
    ),
]


def get_template(key: str) -> SearchTemplate | None:
    for template in TEMPLATES:
        if template.key == key:
            return template
    return None
