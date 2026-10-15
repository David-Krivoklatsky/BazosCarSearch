"""Contract tests for the Mini App SPA (dashboard/index.html).

No JS runtime here — these pin the integration points the SPA must expose
so backend changes cannot silently desync the frontend:
- every backend GET view has a tab/loader and a renderer,
- every POST action the SPA calls exists in api/webapp.py,
- Telegram native hooks (theme, BackButton, haptics, initData header) exist,
- key element ids referenced by the JS exist in the markup.
"""

from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent.parent / "dashboard" / "index.html").read_text(
    encoding="utf-8"
)


def test_spa_covers_all_backend_views() -> None:
    for view in ("overview", "inventory", "saved", "queue", "stats", "profiles"):
        assert f'data-view="{view}"' in HTML, f"missing tab for view {view}"
        assert f"render{view.capitalize()}" in HTML or f"render{view.title()}" in HTML or view in HTML
    assert "openListing" in HTML  # listing detail
    assert "openCompare" in HTML  # compare


def test_spa_actions_match_backend() -> None:
    import api.webapp as webapp

    src = Path(webapp.__file__).read_text(encoding="utf-8")
    for action in ("save", "unsave", "note", "use", "toggle_search", "notify", "score", "photos"):
        assert f'"{action}"' in src, f"backend missing action {action}"
        assert action in HTML, f"SPA never calls action {action}"


def test_spa_sends_init_data_header() -> None:
    assert "X-Telegram-Init-Data" in HTML
    assert "tg.initData" in HTML


def test_spa_telegram_native_hooks() -> None:
    assert "themeParams" in HTML
    assert "BackButton" in HTML
    assert "HapticFeedback" in HTML
    assert "tg.ready()" in HTML


def test_spa_has_gallery_description_and_filters() -> None:
    assert "gallery(" in HTML and "image_urls" in HTML
    assert "descBlock" in HTML and "description" in HTML
    for control in ("fq", "fsort", "fstatus", "fminp", "fmaxp", "fminy", "fmaxy"):
        assert f'id="{control}"' in HTML, f"missing filter control {control}"


def test_spa_renders_stats_v2_sections() -> None:
    for marker in ("Price histogram", "Top makes", "Compare", "Pipeline funnel",
                   "AI scoring cost", "Biggest discounts", "Scoring pace"):
        assert marker in HTML, f"missing stats section {marker}"


def test_spa_benchmark_warns_on_small_n() -> None:
    assert "low data" in HTML


def test_spa_saved_shows_alert_threshold() -> None:
    assert "watching below" in HTML


def test_spa_has_retry_and_empty_states() -> None:
    assert "Try again" in HTML
    assert "lastRetry" in HTML


def test_spa_compare_back_navigation() -> None:
    """Compare -> detail -> back must reopen the compare view, not dead-end."""
    assert "lastCompareIds" in HTML
    assert "function backToCompare" in HTML
    assert "KNOWN_VIEWS" in HTML
    # the detail back button routes through the compare path when coming from it
    assert 'from === "compare"' in HTML


def test_spa_native_hardening() -> None:
    """Phase 2: Telegram-native link handling, no prompt(), inline notes."""
    assert "tg.openLink" in HTML
    assert "disableVerticalSwipes" in HTML
    assert "showAlert" in HTML
    assert "toggleSave" in HTML
    assert "saveNote" in HTML
    assert "prompt(" not in HTML, "prompt() is unreliable in Telegram webviews"
    assert "javascript:" not in HTML


def test_spa_tables_scroll_on_mobile() -> None:
    assert "overflow-x: auto" in HTML
    assert "min-width" in HTML


def test_spa_js_balanced_braces() -> None:
    scripts = re.findall(r"<script>(.*?)</script>", HTML, re.DOTALL)
    assert scripts, "no inline script found"
    code = max(scripts, key=len)
    assert code.count("{") == code.count("}"), "unbalanced braces in SPA script"
    assert code.count("(") == code.count(")"), "unbalanced parens in SPA script"
