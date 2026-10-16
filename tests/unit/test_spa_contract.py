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
    for view in ("listings", "saved", "insights", "profile"):
        assert f'data-view="{view}"' in HTML, f"missing tab for view {view}"
    # legacy tab/view names still resolve (old links, cached pages)
    for legacy in ("overview", "inventory", "queue", "stats", "profiles"):
        assert legacy in HTML, f"legacy view {legacy} not aliased"
    assert "openListing" in HTML  # listing detail
    assert "openCompare" in HTML  # compare
    # full-command parity views (every bot command lives in the Mini App)
    for view in ("bounds", "models", "templates", "health", "digest", "scraper", "rescore_preview"):
        assert f'"{view}"' in HTML, f"SPA never requests view {view}"


def test_spa_actions_match_backend() -> None:
    import api.webapp as webapp

    src = Path(webapp.__file__).read_text(encoding="utf-8")
    for action in ("save", "unsave", "note", "alert", "criteria", "use",
                   "toggle_search", "notify", "score", "photos",
                   "model", "prompt", "fallbacks", "filters", "search_create",
                   "search_delete", "template", "eval", "rescore", "check_ad",
                   "queue_clear", "queue_confirm", "queue_cancel", "rescore_range"):
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
    for control in ("fq", "fsort", "fstatus", "dayRange", "plo", "yla"):
        assert f'id="{control}"' in HTML, f"missing filter control {control}"


def test_spa_listings_layout_and_formats() -> None:
    """Item 7+8: dd.mm dates, xK mileage, score under photo, price bottom-right."""
    assert "fmtDateDM" in HTML
    assert "fmtKmK" in HTML
    assert "photo-col" in HTML
    assert "cardHtml" in HTML
    # infinite scroll instead of pager buttons
    assert "sentinel" in HTML and "loadMore" in HTML
    assert "‹ Prev" not in HTML and "Next ›" not in HTML
    # CSV + window slider live at the top, window button is a slider
    assert "Export CSV" in HTML and "DAY_OPTS" in HTML
    assert "30d window" not in HTML


def test_spa_renders_stats_v2_sections() -> None:
    for marker in ("Price histogram", "Top makes", "Compare", "Pipeline funnel",
                   "AI scoring cost (30 days)", "Biggest discounts (30 days)",
                   "Scoring pace (14 days)", "AI queue"):
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

def test_spa_filter_changes_soft_reload_with_skeletons() -> None:
    """Filter/sider changes must not wipe the page (no black flash)."""
    assert "softReload" in HTML
    # skeletons render on top of the old cards while the new page loads
    assert "afterbegin" in HTML and "skel" in HTML
    # toolbar rebuilds from inv state (no focus/value loss beyond the edit)
    assert "scheduleReload" in HTML
    # infinite-scroll append must not clobber the merged first-page payload
    assert "lastData.items = lastData.items.concat(items)" in HTML


def test_spa_has_scroll_to_top() -> None:
    assert 'id="totop"' in HTML
    assert "scrollTop" in HTML

def test_spa_card_title_spans_full_width() -> None:
    """Title lives in its own full-width row above photo/specs/price."""
    assert "toprow" in HTML and "botrow" in HTML
    assert "photo-col" in HTML


def test_spa_saved_reuses_shared_card() -> None:
    assert 'from: "saved"' in HTML
    assert "cardHtml(r, {" in HTML


def test_spa_drops_open_in_app_detail() -> None:
    """Biggest-discount titles route to the Bazcar detail, not Bazoš."""
    assert "openListing(${x.ad_id}" in HTML

def test_spa_scraper_transparency_surface() -> None:
    assert "scraperHtml" in HTML
    assert "Recent runs" in HTML
    assert "Find a missing car" in HTML
    assert "checkad-in" in HTML and "checkAd" in HTML
    assert "scopeLabel" in HTML


def test_spa_v3_cost_range_and_queue_controls() -> None:
    assert "ratedBy" in HTML and "removedBadge" in HTML  # which AI rated it / removed
    assert "rangePreview" in HTML and "rangeRescore" in HTML  # date-range re-score
    assert "queueConfirm" in HTML and "queueCancel" in HTML and "queueClear" in HTML
    assert "Clear queue" in HTML and "Confirm and score" in HTML
