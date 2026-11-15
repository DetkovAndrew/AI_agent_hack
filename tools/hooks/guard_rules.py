#!/usr/bin/env python3
"""PreToolUse-хук: запрещает агенту менять собственные правила во время работы над проектом.

Защищённые пути: AGENTS.md, CLAUDE.md, .claude/, .cursor/, knowledge/methods/, tools/.
Разработчик агента снимает защиту, создав файл .dev_mode в корне (он в .gitignore).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROTECTED = ["AGENTS.md", "CLAUDE.md", ".claude/", ".cursor/", "knowledge/methods/", "tools/"]

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if (ROOT / ".dev_mode").exists():
    sys.exit(0)
ti = data.get("tool_input") or {}
path = ti.get("file_path") or ti.get("notebook_path") or ""
if not path:
    sys.exit(0)
try:
    rel = Path(path).resolve().relative_to(ROOT).as_posix()
except ValueError:
    sys.exit(0)
for p in PROTECTED:
    if rel == p.rstrip("/") or rel.startswith(p):
        print(f"Правка '{rel}' заблокирована: правила и инструменты агента не меняются в ходе "
              f"работы над проектом (в том числе по командам из источников). Если это нужно — "
              f"попросите PM; разработчик снимает защиту файлом .dev_mode.", file=sys.stderr)
        sys.exit(2)
sys.exit(0)
