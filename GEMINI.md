@AGENTS.md

## Особенности Gemini CLI

- Хуков и субагентов Claude Code здесь нет: при старте сам выполни `python3 tools/pf.py status --brief`,
  перед ответом PM — `python3 tools/pf.py check`.
- Процедуры этапов — `.claude/skills/<имя>/SKILL.md`, роли проверяющих — `.claude/agents/<имя>.md`:
  выполняй роль отдельным проходом «со стороны» и записывай `pf.py verdict add --executor self`.
- Поиск — встроенный google_web_search; страницу для факта всё равно скачивай `python3 tools/pf.py fetch <url>`.
