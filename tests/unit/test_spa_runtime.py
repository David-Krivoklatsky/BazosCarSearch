"""Execute the Mini App SPA in a real JS engine (``node --test``).

``tests/js/spa.test.mjs`` runs the shipped ``dashboard/index.html`` script
inside a stubbed DOM and asserts on its pure rendering helpers. Text-level
contract tests (``test_spa_contract.py``) cannot catch a syntax error or a
broken formatter — which is why this shells out to node. It is skipped when
node is not installed, mirroring how the offline suite tolerates missing tools.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_JS_TESTS = sorted((_REPO_ROOT / "tests" / "js").glob("*.test.mjs"))


def test_spa_js_runtime_suite() -> None:
    assert _JS_TESTS, "no JS test files found under tests/js"
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available — skipping SPA JS runtime tests")
    result = subprocess.run(
        [node, "--test", *[str(p) for p in _JS_TESTS]],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "fail 0" in result.stdout, f"unexpected node --test summary:\n{result.stdout}"
