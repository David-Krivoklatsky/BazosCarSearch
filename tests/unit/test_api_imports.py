"""Guard: the Vercel runtime paths must not import scraper-only deps.

``requirements.txt`` (what Vercel installs) intentionally omits
``beautifulsoup4`` / ``lxml`` — only the webhook + Mini App API run there.
This test imports everything those functions touch in a subprocess where the
scraper deps are unimportable, so a stray ``import bazcar.scrapers`` (or a
``bs4`` import) creeping into the bot/API import chain fails loudly here
instead of at cold start in production.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = """
import sys

_BLOCKED = {"bs4", "lxml", "soupsieve"}


class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in _BLOCKED:
            raise ImportError(f"scraper-only dep {name!r} must not be imported on Vercel")
        return None


sys.meta_path.insert(0, _Blocker())

# Everything the webhook (api/telegram.py) and Mini App API (api/webapp.py)
# import at call time:
import bazcar.bot.daemon  # noqa: E402,F401
import bazcar.bot.store  # noqa: E402,F401
import bazcar.core.profiles  # noqa: E402,F401
import bazcar.db.repository  # noqa: E402,F401
import bazcar.notify.telegram  # noqa: E402,F401

# Load the single Vercel entrypoint by path (as the runtime does) and route
# every surface through it.
import importlib.util  # noqa: E402
import io  # noqa: E402
import os  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_api_index", os.path.join(os.getcwd(), "api", "index.py")
)
_entry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_entry)


def _route(path, method="GET"):
    caps = []
    _entry.app(
        {"REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO(b"")},
        lambda status, headers: caps.append(status),
    )
    return caps[0]


assert _route("/api/dashboard").startswith("200")
assert _route("/api/webapp").startswith("401")
assert _route("/api/telegram").startswith("405")

leaked = [m for m in sys.modules if m.split(".")[0] in _BLOCKED]
assert not leaked, f"scraper deps imported by bot/API chain: {leaked}"
assert "bazcar.scrapers.bazos" not in sys.modules, "scraper stack pulled into Vercel path"
print("OK")
"""


def test_vercel_import_chain_avoids_scraper_deps() -> None:
    env = dict(os.environ)
    src = str(_REPO_ROOT / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip().endswith("OK")
