"""Зафиксированные `raw_json` реальных листингов — см. README.md."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).parent

GOOD = "text_lines_good.json"
SIDEBAR = "text_lines_sidebar.json"


def load(name: str) -> dict[str, Any]:
    """Прочитать фикстуру по имени файла (`GOOD` / `SIDEBAR`)."""
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
