@AGENTS.md

## Особенности Claude Code

- Хук `SessionStart` показывает статус активного проекта — продолжай с «Следующего действия».
- Хук `Stop` запускает `pf.py check`; при ошибках целостности он не даёт закончить ответ и
  присылает их тебе — исправь сразу или объясни PM, почему сейчас нельзя.
- Хук `PreToolUse` запрещает правку правил агента; для разработки агента создайте файл `.dev_mode`.
- Процедуры этапов — skills `/pf-start`, `/pf-intake`, `/pf-collect`, `/pf-research`, `/pf-options`,
  `/pf-strategy`, `/pf-validation`, `/pf-decision`, `/pf-revise`, `/pf-interview`, `/pf-status`.
- Субагенты — `.claude/agents/`: researcher, source-extractor, data-analyst, red-team,
  synthetic-respondent, judge. Передавай им ID проекта и вопроса, а не весь контекст.
- Команды `pf.py` запускай как `python3 tools/pf.py ...` из корня репозитория.
