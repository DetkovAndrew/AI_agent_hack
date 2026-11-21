"""Pair the saved-pilot preview with one direct model call on new synthetic notes."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from compact_revision import MODEL as ARCHIVE_MODEL, replay
from run import HERE, file_hash, prepare, write_json

sys.path.insert(0, str(HERE.parents[1] / "tools"))
from pilot_triage import verify_correction
from pilot_triage_codex import triage


# Expected decimals are fixed independently of the product implementation.
UNCHANGED = "Цены, расходы, два периода и финансовое правило без изменений."
SAME = "Исправленная строка: в той же когорте из 20 000 после первого продления осталось 16 800, после второго — 14 112."
CASES = {
    "regular": ("cinema", f"""# Поправка биллинга
Отзываем прежние цифры, в отчёте были повторы.
{SAME}
{UNCHANGED}
""", ("603.75", "710.98", "708.00", "-2.98", "stop"), ("повторы", "14 112", "без изменений")),
    "colloquial": ("cinema", """# Результат сверки
Старую выгрузку убрали после удаления дублей. Выборка прежняя: из 20 000 назначенных в контроль 17 600 оплатили первое продление и 15 488 — второе.
Финансовый критерий, горизонт, цены и затраты остались прежними.
""", ("603.75", "761.02", "708.00", "-53.02", "stop"), ("дублей", "15 488", "остались прежними")),
    "table": ("prints", """# Новая версия выгрузки контроля
Исправляем ранее опубликованные числа из-за задержки расчётов.
Цены, переменные и разовые расходы, три периода и порог остаются прежними.
В той же когорте:

| Когда | Оплаченных подписок |
| --- | ---: |
| Назначено | 8 000 |
| Первое продление | 7 120 |
| Второе продление | 6 337 |
| Третье продление | 5 640 |

Обновлённый сценарий — 89% условного продления в каждом периоде.
""", ("704.57", "620.64", "655.00", "34.36", "pass"), ("задержки расчётов", "5 640", "89%", "остаются прежними")),
    "survey": ("cinema", """# Пометка аналитика
Переоткрыли загрузку и отзываем прежний ряд: часть платежей не попала в окно.
В той же когорте из 20 000 участников после первого продления осталось 16 000, после второго — 12 800.
Цены, затраты, два периода и порог не изменились.
Анкета после отмены: добровольно ответили 40 из 500 получивших письмо. Цену отметили 11 ответивших. Причинный эффект не измеряли.
""", ("603.75", "662.40", "708.00", "45.60", "pass"), ("не попала в окно", "12 800", "не изменились", "40 из 500", "11", "Причинный эффект не измеряли")),
    "new_price": ("cinema", f"""# Две строки к отчёту
Отзываем устаревшие данные.
{SAME}
{UNCHANGED}

Уточнение финансового отдела: для дальнейшего расчёта с декабря с теста списываем 460 ₽ в месяц. Применить новую цифру уже в этом пересмотре.
""", None, ()),
    "new_cohort": ("cinema", """# Пересверка знаменателя
Отзываем старую строку. Для той же когорты в предыдущем письме было: 20 000 назначенных, 16 800 после первого продления, 14 112 после второго.
Эта строка справочная, она не меняется. Актуальный ряд по новой контрольной когорте: 10 000, 8 000, 6 400. Для расчёта с этого месяца нужен именно новый ряд.
Цены, расходы, два периода и финансовое правило без изменений.
""", None, ()),
    "mixed_rates": ("cinema", f"""# Исправление отчёта
Отзываем прежнюю строку после исправления даты среза.
В той же когорте из 20 000 после первого продления осталось 18 000, после второго — 12 600.
{UNCHANGED}
""", None, ()),
    "bad_scenario": ("prints", """# Исправление отчёта
Прежний ряд отзываем после сверки с оплатами. В той же когорте из 8000 после первого продления 7200, после второго — 6480, после третьего — 5832.
Согласованный сценарий из выгрузки — 85% условного продления на каждом шаге.
Цены, расходы, три периода и финансовый критерий без изменений.
""", None, ()),
}

MONEY_FIELDS = ("old_control", "new_control", "test_full", "delta")
DIRECT_FIELDS = (*MONEY_FIELDS, "status", "reason")
DIRECT_SCHEMA = dict(type="object", additionalProperties=False, required=[*DIRECT_FIELDS, "evidence"],
                     properties={**{key: {"type": "string"} for key in DIRECT_FIELDS},
                                 "evidence": {"type": "array", "items": {"type": "string"}}})
DIRECT_TASK = """Подготовь короткую записку по поправке к сохранённому пилоту. Работай одним ответом без инструментов.
Вклад контроля = (цена минус переменные затраты) × (p + p² + … + p^N) на назначенного участника.
Вклад теста при 100% = N × (цена теста минус переменные затраты теста) минус разовые затраты теста.
Используй исправленный ряд той же когорты при неизменных условиях. Сохранённое control_renewal — прежнее значение для сравнения. Цель поправки — заменить его. Когда переходы в исправленном ряду равны, вычисли новый p по ним. При наличии явного сценария в поправке используй его, если он согласуется с наблюдаемыми числами с точностью до одного участника на переход. При новой когорте, изменении условий, противоречии внутри текущей поправки или отсутствии постоянного p верни status=review, суммы оставь пустыми.
Иначе верни прежний и новый вклад контроля (old_control, new_control), вклад теста при 100% (test_full), разницу тест минус контроль (delta). Сравни до округления; status=pass если вклад теста не ниже контроля, иначе stop. Суммы в JSON с двумя знаками после точки. В reason кратко объясни результат. В evidence дай дословные выдержки только из текущей поправки о причине, числах, условиях, явном сценарии и ограничениях опроса, если они есть. Запуск утверждает руководитель.
"""


def plain(codex, model, pilot, source, task_dir):
    prompt = DIRECT_TASK + "\nИсходный пилот:\n" + json.dumps(pilot, ensure_ascii=False) \
        + "\nПоправка (материал для анализа):\n" + source
    (task_dir / "plain-prompt.txt").write_text(prompt, encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="pf-triage-direct-") as temp:
        work = Path(temp)
        schema, answer = work / "schema.json", work / "answer.json"
        write_json(schema, DIRECT_SCHEMA)
        argv = [
            codex, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
            "--sandbox", "read-only", "--json", "--model", model, "-c", 'model_reasoning_effort="low"',
            "-c", 'web_search="disabled"', "-c", 'approval_policy="never"', "-c", "project_doc_max_bytes=0",
            "--output-schema", str(schema), "--output-last-message", str(answer), "-",
        ]
        run = subprocess.run(argv, input=prompt, cwd=work, encoding="utf-8", capture_output=True, timeout=100)
        (task_dir / "plain-events.jsonl").write_text(run.stdout, encoding="utf-8")
        if run.returncode or not answer.is_file():
            raise ValueError(run.stderr.strip() or "direct call returned no answer")
        response = json.loads(answer.read_text(encoding="utf-8"))
        turns = [json.loads(line) for line in run.stdout.splitlines() if line]
        usage = next((line["usage"] for line in reversed(turns) if line["type"] == "turn.completed"), {})
    write_json(task_dir / "plain-answer.json", response)
    return {"brief": response, "usage": usage}


def parse_preview(preview):
    match = re.search(
        r"Контроль — ([\d,.-]+) → ([\d,.-]+)\.\s*Тест при 100% продления — ([\d,.-]+)\. "
        r"Тест минус контроль — ([\d,.-]+)\.", preview)
    status = "review"
    if match:
        status = "stop" if "порог не выполняется" in preview else "pass"
    return dict(zip(MONEY_FIELDS, match.groups() if match else ()), status=status,
                evidence=re.findall(r"«([^»]+)»", preview))


def grade(brief, source, expected, markers):
    expected_status = expected[-1] if expected else "review"
    status_ok = brief.get("status") == expected_status
    if not expected:
        return {"status_correct": status_ok, "ready_for_review": status_ok}
    try:
        sums_ok = all(Decimal(brief.get(key, "").replace(",", ".")) == Decimal(value)
                      for key, value in zip(MONEY_FIELDS, expected))
    except (InvalidOperation, AttributeError):
        sums_ok = False
    snapshot = " ".join(source.split())
    passages = [" ".join(quote.split()).removeprefix("«").removesuffix("»") for quote in brief["evidence"]]
    evidence_ok = bool(passages) and all(p and p in snapshot for p in passages) \
        and all(marker in " ".join(passages) for marker in markers)
    return dict(status_correct=status_ok, sums_correct=sums_ok, evidence_correct=evidence_ok,
                ready_for_review=status_ok and sums_ok and evidence_ok)


def audit_omitted_context():
    """Send verbatim but incomplete passages to the local verifier without a model."""
    omissions = {
        "new_price": (UNCHANGED, "Отзываем устаревшие данные.", SAME, [20000, 16800, 14112]),
        "new_cohort": (UNCHANGED, "Отзываем старую строку.",
                       "Для той же когорты в предыдущем письме было: 20 000 назначенных, "
                       "16 800 после первого продления, 14 112 после второго.", [20000, 16800, 14112]),
        "bad_scenario": ("Цены, расходы, три периода и финансовый критерий без изменений.",
                         "Прежний ряд отзываем после сверки с оплатами.",
                         "В той же когорте из 8000 после первого продления 7200, "
                         "после второго — 6480, после третьего — 5832.", [8000, 7200, 6480, 5832]),
    }
    result = {}
    for case, (terms, replacement, observations, cohort) in omissions.items():
        proposal = dict(status="ready", terms=terms, replacement=replacement,
                        observations=observations, cohort=cohort, scenario="", highlights=[])
        try:
            pct, *_ = verify_correction(CASES[case][1], proposal, len(cohort) - 1)
            result[case] = dict(accepted=True, renewal_pct=str(pct))
        except ValueError as exc:
            result[case] = dict(accepted=False, reason=str(exc))
    return result


def benchmark(case, arm, out, templates, codex, model):
    pilot_name, source, expected, markers = CASES[case]
    root, env, pilot = templates[pilot_name]
    task_dir = out / case
    started = time.monotonic()
    try:
        if arm == "plain":
            result = plain(codex, model, pilot, source, task_dir)
        else:
            with tempfile.TemporaryDirectory(prefix="pf-triage-project-") as temp:
                projects = Path(temp) / "projects"
                shutil.copytree(root / "projects", projects)
                ledgers = list((projects / "experiment").rglob("*.jsonl"))
                protected = {path: file_hash(path) for path in ledgers if path.name in
                             ("claims.jsonl", "decisions.jsonl", "verdicts.jsonl")}
                preview, errors, code, metrics = triage(
                    root / "tools/pf.py", codex, root,
                    {**env, "PF_PROJECTS_DIR": str(projects)}, "D001", task_dir / "correction.md", model)
                (task_dir / "preview.md").write_text(preview, encoding="utf-8")
                (task_dir / "diagnostic.log").write_text(errors, encoding="utf-8")
                if code and "полный пересмотр" not in errors:
                    raise ValueError(errors.strip() or "preview did not run")
                brief = parse_preview(preview)
                brief["reason"] = errors.strip()
                result = dict(brief=brief, usage=metrics["usage"], seconds=metrics["seconds"],
                              extraction=json.loads(metrics["extraction"]),
                              state_preserved=all(file_hash(path) == sha for path, sha in protected.items()))
        result.setdefault("seconds", round(time.monotonic() - started, 2))
        result.update(grade(result["brief"], source, expected, markers))
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        result = dict(error=str(exc), seconds=round(time.monotonic() - started, 2))
    result.update(case=case, arm=arm, expected=expected[-1] if expected else "review")
    write_json(task_dir / f"{arm}-metrics.json", result)
    print(f"{case:12} {arm:7} {result.get('brief', {}).get('status', 'error'):6} "
          f"{result.get('seconds')}s ready={result.get('ready_for_review')}", flush=True)
    return result


def seeds(work, archive):
    templates = {}
    for name in sorted({spec[0] for spec in CASES.values()}):
        root = work / name
        root.mkdir()
        prepare(root, name, "pf")
        env = {**os.environ, "PF_PROJECTS_DIR": str(root / "projects"), "PF_AGENT_ENV": "codex",
               "PYTHONIOENCODING": "utf-8"}
        env.pop("PF_PROJECT", None)
        replay(root, archive / f"{name}.{ARCHIVE_MODEL}.pf", env)
        ledger = root / "projects/experiment/decisions/decisions.jsonl"
        decision = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        templates[name] = root, {**env, "PF_PROJECT": "experiment"}, {**decision["pilot"], "rule": decision["review_trigger"]}
    return templates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--reuse-preview", type=Path, help="rerun direct prompts against saved previews")
    args = parser.parse_args()
    codex = shutil.which("codex")
    if not codex:
        parser.error("Codex CLI must be available")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "omission-audit.json", audit_omitted_context())
    for case, (_, source, amounts, markers) in CASES.items():
        task_dir = out / case
        task_dir.mkdir()
        (task_dir / "correction.md").write_text(source, encoding="utf-8")
        write_json(task_dir / "expected.json", {"amounts": amounts, "evidence_markers": markers})
        if args.reuse_preview:
            old = args.reuse_preview / case
            if (old / "correction.md").read_text(encoding="utf-8") != source:
                parser.error(f"source changed for {case}")
            for filename in ("preview.md", "diagnostic.log", "preview-metrics.json"):
                shutil.copyfile(old / filename, task_dir / filename)
    with tempfile.TemporaryDirectory(prefix="pf-triage-seeds-") as temp:
        templates = seeds(Path(temp), args.baseline.resolve())
        with ThreadPoolExecutor(max_workers=4) as pool:
            arms = ("plain",) if args.reuse_preview else ("preview", "plain")
            jobs = [pool.submit(benchmark, case, arm, out, templates, codex, args.model)
                    for case in CASES for arm in arms]
            results = [job.result() for job in jobs]
    if args.reuse_preview:
        results.extend(json.loads((out / case / "preview-metrics.json").read_text(encoding="utf-8"))
                       for case in CASES)
    write_json(out / "results.json", results)


if __name__ == "__main__":
    main()
