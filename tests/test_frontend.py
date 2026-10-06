"""Catch typos between the page and its scripts: every #id the JavaScript looks up must exist."""

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def test_every_element_the_scripts_use_exists():
    html = (FRONTEND / "index.html").read_text()
    ids = set(re.findall(r'id="([^"]+)"', html))
    used = set()
    for script in ("app.js", "chat.js", "store.js"):
        used |= set(re.findall(r'\$\("#([\w-]+)"\)', (FRONTEND / script).read_text()))
    assert not used - ids, f"missing in index.html: {sorted(used - ids)}"


def test_page_loads_every_script():
    html = (FRONTEND / "index.html").read_text()
    for script in ("store.js", "app.js", "chat.js"):
        assert f'<script src="{script}">' in html
