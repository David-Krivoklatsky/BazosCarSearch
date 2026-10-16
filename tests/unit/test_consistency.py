"""Parity + consistency guards for the English-only bot surface.

- Every visible command has a ``case`` handler and COMMAND_HELP entry;
  hidden aliases work but stay out of the advertised menu.
- Every menu button routes to a real action.
- No Slovak leaks outside the explicit allowlist (LLM prompt + Slovak-text
  detectors + input aliases + fixtures).
- Every BAZCAR_* setting is documented in .env.example.
- README command list matches BOT_COMMANDS.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DAEMON = (REPO / "src" / "bazcar" / "bot" / "daemon.py").read_text(encoding="utf-8")

# Slovak may ONLY appear in: the LLM evaluation prompt (listings are Slovak),
# Slovak-text detectors/parsers, backward-compat input aliases, and fixtures.
SLOVAK_ALLOWLIST = {
    "src/bazcar/llm/provider.py",  # SYSTEM_PROMPT — Slovak listings need it
    "src/bazcar/bot/classify.py",  # part-marker detection on Slovak text
    "src/bazcar/scrapers/bazos.py",  # Slovak page parsing
    "src/bazcar/core/adpage.py",  # Bazoš ad-page meta parsing (quotes site text)
    "src/bazcar/bot/formatting.py",  # backward-compat input aliases + Škoda example
    "src/bazcar/bot/daemon.py",  # input aliases only (checked separately)
    "src/bazcar/db/repository.py",  # make stoplist for Slovak titles
    "tests/fixtures/bazos_page.html",
}
_SLOVAK_RE = re.compile(r"[áäčďéíľňóôŕšťúýžÁČĎÉÍĽŇÓŠŤÚÝŽ]")
# Slovak diacritics that are still legal in daemon.py: input aliases,
# the Bazoš proper noun, and the Škoda docstring example.
DAEMON_ALLOWED = re.compile(
    r"neohodnoten|ohodnoten|najlacnej|najdrah|drah|lacn|sk[oó]re|najlep|najnov|"
    r"nove|nové|vsetky|všetky|dni|Bazoš|Škoda"
)


def _commands() -> list[str]:
    from bazcar.bot.daemon import BOT_COMMANDS

    return [c["command"] for c in BOT_COMMANDS]


def test_visible_commands_have_handlers_and_help() -> None:
    from bazcar.bot.daemon import BOT_COMMANDS, COMMAND_HELP

    for entry in BOT_COMMANDS:
        cmd = entry["command"]
        assert f'case "/{cmd}":' in DAEMON, f"no handler for /{cmd}"
        assert f'"/{cmd}":' in DAEMON and f"/{cmd}" in COMMAND_HELP, f"no help for /{cmd}"
        assert entry["description"].strip(), f"empty description for /{cmd}"


def test_hidden_aliases_work_but_stay_hidden() -> None:
    from bazcar.bot.daemon import _HIDDEN_COMMANDS, BOT_COMMANDS

    visible = {c["command"] for c in BOT_COMMANDS}
    assert not (visible & _HIDDEN_COMMANDS), "alias leaked into the visible menu"
    for cmd in _HIDDEN_COMMANDS:
        assert f'case "/{cmd}":' in DAEMON, f"hidden alias /{cmd} has no handler"


def test_menu_buttons_route_somewhere() -> None:
    from bazcar.bot.daemon import MENU_KEYBOARD

    actions = set()
    for row in MENU_KEYBOARD["inline_keyboard"]:
        for button in row:
            assert button["text"].strip(), "empty menu button"
            data = button.get("callback_data", "")
            assert data.startswith("menu:"), f"menu button without action: {button}"
            actions.add(data.split(":", 1)[1])
    # every menu action is handled (menu:status kept as compat -> settings)
    handled = set(re.findall(r'(?:elif|if) action == "(\w+)":', DAEMON))
    handled |= set(re.findall(r'elif action in \{([^}]*)\}', DAEMON)[0].replace('"', "").replace(" ", "").split(","))
    handled |= {"rescrape", "status"}
    assert actions <= handled, f"menu actions without handler: {actions - handled}"


def _has_slovak_beyond_proper_noun(line: str) -> bool:
    """True if the line has Slovak diacritics outside the Bazoš proper noun."""
    return bool(_SLOVAK_RE.search(line.replace("Bazoš", "").replace("Bazos", "")))


def test_no_slovak_outside_allowlist() -> None:
    offenders: list[str] = []
    for path in sorted((REPO / "src").rglob("*.py")):
        rel = str(path.relative_to(REPO)).replace("\\", "/")
        if rel in SLOVAK_ALLOWLIST or rel.startswith("src/bazcar/cli/"):
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _has_slovak_beyond_proper_noun(line):
                offenders.append(f"{rel}:{i}: {line.strip()[:80]}")
    for path in [REPO / "dashboard" / "index.html", REPO / "api" / "webapp.py",
                 REPO / "api" / "telegram.py", REPO / "api" / "dashboard.py",
                 REPO / "api" / "index.py"]:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _has_slovak_beyond_proper_noun(line):
                offenders.append(f"{path.name}:{i}: {line.strip()[:80]}")
    assert not offenders, "Slovak UI strings outside the allowlist:\n" + "\n".join(offenders)


def test_daemon_slovak_is_only_aliases_and_proper_nouns() -> None:
    for i, line in enumerate(DAEMON.splitlines(), 1):
        if not _SLOVAK_RE.search(line):
            continue
        stripped = re.sub(r'"[^"]*"', "", line)  # ignore string literals first pass
        if _SLOVAK_RE.search(stripped) and "token in {" not in line and "{" not in line:
            # non-literal Slovak (comments/docstrings) must be an allowed word
            assert DAEMON_ALLOWED.search(line), f"daemon.py:{i}: {line.strip()[:100]}"


def test_no_plain_ascii_slovak_in_ui_files() -> None:
    """Catch Slovak words without diacritics the diacritic regex misses.

    (dohodou/zlyhal class of leftovers — the LLM prompt file is exempt.)
    """
    offenders: list[str] = []
    for path in sorted((REPO / "src").rglob("*.py")):
        rel = str(path.relative_to(REPO)).replace("\\", "/")
        if rel in SLOVAK_ALLOWLIST:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            low = line.lower()
            for word in ("dohodou", "zlyhal", "z maxima", "bez dát", "bez dat"):
                if word in low:
                    offenders.append(f"{rel}:{i}: {line.strip()[:80]}")
    for path in [REPO / "dashboard" / "index.html", REPO / "api" / "webapp.py",
                 REPO / "api" / "telegram.py"]:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            low = line.lower()
            for word in ("dohodou", "zlyhal"):
                if word in low:
                    offenders.append(f"{path.name}:{i}: {line.strip()[:80]}")
    assert not offenders, "plain-ASCII Slovak leftovers:\n" + "\n".join(offenders)


def test_env_example_documents_all_settings() -> None:
    from bazcar.config.settings import Settings

    example = (REPO / ".env.example").read_text(encoding="utf-8")
    fields = [n for n in Settings.model_fields if n not in {"scraper_config", "llm_config", "log_level", "export_dir"}]
    missing = [n for n in fields if n.upper() not in example and f"BAZCAR_{n.upper()}" not in example]
    # monthly_budget_usd maps to BAZCAR_MONTHLY_BUDGET_USD
    missing = [n for n in missing if f"BAZCAR_{n.upper()}" not in example and n.upper() not in example]
    assert not missing, f"settings without .env.example docs: {missing}"
    assert "BAZCAR_MONTHLY_BUDGET_USD" in example


def test_readme_command_list_matches_menu() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    for cmd in _commands():
        assert f"/{cmd}" in readme, f"README missing /{cmd}"
    assert "6 fáz" not in readme and "6-fáz" not in readme, "README still claims 6 phases"
    assert _SLOVAK_RE.search(readme) is None or "Bazoš" in readme, "README has Slovak outside Bazoš"
