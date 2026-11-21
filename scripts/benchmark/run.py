"""Paired product-strategy benchmark using the same Codex CLI in both arms."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
import hashlib
import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DEFAULT_CASES = ("renewal", "onboarding")
PILOT_CASES = ("cinema", "prints")
CASES = DEFAULT_CASES + PILOT_CASES
MODELS = ("gpt-6-luna", "gpt-6.2-sol")
ARMS = ("plain", "pf")

COMMON = """Это учебная задача для директора по продукту. Работай только через локальные
MCP-инструменты. Готовые материалы открывает отдельный инструмент read_case_material с аргументом name.
Вычисления — через calculate. Документ сохраняет отдельный инструмент save_director_answer с аргументом markdown.
Интернет и данные других проектов не используй.
Сохрани итог для директора через save_director_answer, на русском, до 750 слов. Существенные выводы
свяжи с конкретными исходными данными. Укажи, что известно и что предстоит проверить.
Решение согласует директор, не выдавай условную рекомендацию за его одобрение.
Не задавай уточняющих вопросов: зафиксируй существенные пробелы и дай условный ответ.
"""
INITIAL = """Прочитай исходные материалы через read_case_material с name=initial. Сделай быстрый разбор: рекомендация и обоснование,
как минимум три варианта, продуктовая и коммуникационная части, план проверки, числовые
условия продолжения или остановки. Сохрани решение и основания так, чтобы новый чат мог продолжить."""
UPDATE = """Продолжим работу с сохранённым решением и основаниями. Через read_case_material прочитай update и previous_answer.
Применяй тот же критерий решения. Покажи, какие данные исправлены, что меняется в рекомендации
и плане и почему. Сохрани актуальный результат и явно покажи, какой вывод больше не действует.
Сохрани в новом ответе относящиеся к решению ограничения и проверку обещаний."""


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(work, case, arm):
    (work / "inbox").mkdir()
    shutil.copyfile(HERE / "serve.py", work / "_bench_server.py")
    shutil.copyfile(HERE / "cases" / case / "initial.md", work / "inbox" / "initial.md")
    if arm == "pf":
        shutil.copyfile(ROOT / "AGENTS.md", work / "AGENTS.md")
        shutil.copyfile(ROOT / "pf.ps1", work / "pf.ps1")
        for directory in ("tools", "knowledge", ".claude/skills", ".claude/agents"):
            shutil.copytree(ROOT / directory, work / directory, ignore=shutil.ignore_patterns("__pycache__"))


def event_records(path):
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    records = []
    for line in lines:
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if line.endswith("\n"):
                raise
            # Killing a timed-out process can interrupt the final write.
    return records


def phase_prompt(arm, case, task):
    prompt = COMMON + "\n" + task
    if arm == "pf":
        prompt += ("\nПроект для этого стенда — experiment. Команды реестра доступны как pf и pf_batch, "
                   "методики — через guide. Прочитай статус командой pf до начала работы. "
                   "Чтобы зарегистрировать исходники, передавай pf ingest inbox/initial.md или inbox/update.md.")
        if case in PILOT_CASES:
            prompt += """
Избегай повторов. В первой фазе одним pf_batch запиши: init experiment --title …,
set brief.depth quick, question add «Финансовый допуск, риски и решение», ingest inbox/initial.md.
Затем одним пакетом запиши факты с --src S001 --quote «дословно из материала» --q Q001.
Раздели цену, затраты, горизонт и порог (фиксированные [C]) и числа контрольной когорты
(отдельный наблюдаемый [C]). Расчётный процент не записывай как факт источника.
Создай decision add с заголовком и текстом исходного порога через --trigger, затем decision pilot.
Первый расчёт: decision pilot --id D001 --months <N> --control-price <цена> --test-price <цена>
--control-cost <затраты> --test-cost <затраты> --test-upfront <затраты>
--test-renewal <нижний сценарий> <верхний> --fixed-basis <C условий> --control-renewal <процент>
--basis <C о контрольной когорте>. На этапе пересмотра прочитай decision show --id D001,
одним пакетом зарегистрируй поправку, запиши факт с дословной цитатой и отзови исправленный [C],
затем передай decision pilot --id D001 --control-renewal <процент> --basis <новый C>.
В тексте объясни, какое решение следует из [D001], что меняется и почему.
Финансовый блок с горизонтом, суммами и статусом допуска добавит report. Не переноси эти числа
в авторский текст. Укажи [D001] и опорные [C]. Остальные наблюдаемые числа приводи по источнику.
Сохрани итог с «## Ответ» и таблицей «## Сверка оснований рекомендации»
(Вывод | Основание | Что может опровергнуть | Когда меняем решение).
Сверь сохранённый итог и финансовый блок по каждой из четырёх ролей. Для просмотра доступен
verdict context --target deliverables/summary.md. Запиши выводы одним pf_publish.
Если выпуск остановлен, исправь конкретные ошибки и запиши проверки исправленной версии.
"""
    return prompt


def publish_report(work, out, env, case, name):
    """Run the product's release gate and archive the exact Markdown and HTML it produces."""
    project = work / "projects" / "experiment"
    authored = project / "deliverables" / "summary.md"
    if not authored.is_file():
        return "missing_summary", None, 0.0
    started = time.monotonic()
    args = [sys.executable, str(work / "tools/pf.py"), "--project", "experiment", "report", "--html"]
    log = out / f"{name}.release.log"
    with log.open("wb") as output:
        try:
            code = subprocess.run(args, cwd=work, env=env, stdout=output, stderr=subprocess.STDOUT,
                                  timeout=45).returncode
        except subprocess.TimeoutExpired:
            code = 124
    seconds = round(time.monotonic() - started, 2)
    if code:
        outcome = {3: "release_blocked", 124: "release_timeout"}.get(code, "release_error")
        return outcome, code, seconds
    report = authored.with_name("REPORT.md")
    html = report.with_suffix(".html")
    if not report.is_file() or not html.is_file():
        return "missing_report", code, seconds
    shutil.copyfile(report, out / f"{name}.report.md")
    shutil.copyfile(html, out / f"{name}.report.html")
    main_text = report.read_text(encoding="utf-8").rsplit("\n## Приложение А. Реестр утверждений\n", 1)[0].rstrip()
    if case in PILOT_CASES:
        ledger = project / "decisions" / "decisions.jsonl"
        decisions = event_records(ledger) if ledger.is_file() else []
        if ("## Финансовый допуск пилота\n" not in main_text or
                not any(d.get("pilot") and d.get("status") in ("active", None) for d in decisions)):
            return "missing_financial_block", code, seconds
    # The director reads the main report. Appendices remain in the complete archived artifact.
    (out / f"{name}.md").write_text(main_text + "\n", encoding="utf-8")
    (work / f"{name}.md").write_text(main_text + "\n", encoding="utf-8")
    return "published", code, seconds


def phase(codex, work, out, env, model, arm, case, name, task, timeout, *, prompt_override=None):
    answer = f"{name}.md"
    prompt = phase_prompt(arm, case, task) if prompt_override is None else prompt_override
    server = "pf-strategist" if arm == "pf" else "benchmark"
    mcp_args = ["-X", "utf8", str(work / "_bench_server.py"), str(work), arm, name]
    (out / f"{name}.prompt.txt").write_text(prompt + "\n", encoding="utf-8")
    args = [codex, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
            "--sandbox", "read-only", "--json", "--model", model, "-c", 'model_reasoning_effort="low"',
            "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
            "-c", f"project_doc_max_bytes={28000 if arm == 'pf' else 0}",
            "-c", f"mcp_servers.{server}.command={json.dumps(sys.executable, ensure_ascii=False)}",
            "-c", f"mcp_servers.{server}.args={json.dumps(mcp_args, ensure_ascii=False)}",
            "-c", f"mcp_servers.{server}.required=true",
            "-c", f'mcp_servers.{server}.default_tools_approval_mode="approve"',
            "--output-last-message", str(out / f"{name}.message.md"), "-"]
    start = time.monotonic()
    errors_path = out / f"{name}.stderr.log"
    events_path = out / f"{name}.events.jsonl"
    with events_path.open("wb") as events, errors_path.open("wb") as errors:
        try:
            proc = subprocess.run(args, cwd=work, env=env, input=prompt.encode("utf-8"),
                                  stdout=events, stderr=errors, timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            exit_code = 124
    elapsed = round(time.monotonic() - start, 2)
    events = event_records(events_path)
    tools = [event["item"] for event in events if event["type"] == "item.completed" and
             event["item"]["type"] in ("command_execution", "file_change", "mcp_tool_call", "web_search")]
    completed = next((event["usage"] for event in reversed(events)
                      if event["type"] == "turn.completed"), {})
    written = (work / answer).is_file()
    usage_limit = any("usage limit" in (event.get("error") or {}).get("message", "").lower()
                      for event in events if event["type"] == "turn.failed")
    release_code, release_seconds = None, 0.0
    if written:
        suffix = f"{name}.draft.md" if arm == "pf" else answer
        shutil.copyfile(work / answer, out / suffix)
    if usage_limit:
        outcome = "usage_limit"
    elif exit_code == 124:
        outcome = "timeout"
    elif exit_code:
        outcome = "model_error"
    elif not written:
        outcome = "missing_answer"
    elif arm == "pf":
        outcome, release_code, release_seconds = publish_report(work, out, env, case, name)
    else:
        outcome = "saved"
    if arm == "pf" and written and name == "initial" and outcome != "published":
        path = work / answer
        draft = path.read_text(encoding="utf-8")
        path.write_text("> Черновик первой фазы. Отчёт не прошёл выпуск.\n\n" + draft, encoding="utf-8")
    result = dict(seconds=round(elapsed + release_seconds, 2), model_seconds=elapsed,
                  release_seconds=release_seconds, exit_code=exit_code, usage=completed, answer_file=written,
                  outcome=outcome, release_code=release_code,
                  tool_calls=len(tools), failed_tools=sum(item.get("status") == "failed" or
                  bool(item.get("exit_code")) for item in tools),
                  usage_limit=usage_limit)
    write_json(out / f"{name}.metrics.json", result)
    print(f"{out.name} {name}: {result['seconds']}s, {outcome}, tools={len(tools)}", flush=True)
    return result


def reuse_plain_phase(previous, out, work, case, name, task):
    if previous is None:
        return None
    directory = previous / out.name
    path = directory / f"{name}.metrics.json"
    if not path.is_file():
        return None
    result = json.loads(path.read_text(encoding="utf-8"))
    if result["exit_code"] or not result["answer_file"] or not result["usage"]:
        return None
    prompt = directory / f"{name}.prompt.txt"
    answer = directory / f"{name}.md"
    if not prompt.is_file() or not answer.is_file() or \
            prompt.read_text(encoding="utf-8") != phase_prompt("plain", case, task) + "\n":
        return None
    for artifact in directory.glob(f"{name}.*"):
        shutil.copyfile(artifact, out / artifact.name)
    shutil.copyfile(answer, work / answer.name)
    result.update(outcome="saved", reused=True, model_seconds=result["seconds"], release_seconds=0.0, release_code=None)
    write_json(out / path.name, result)
    print(f"{out.name}: reusing completed {name} phase", flush=True)
    return result


def run_pair_arm(codex, root, case, model, arm, timeout, previous, reuse_revision):
    label = f"{case}.{model}.{arm}"
    out = root / label
    out.mkdir()
    with tempfile.TemporaryDirectory(prefix="pf-benchmark-") as tmp:
        work = Path(tmp)
        prepare(work, case, arm)
        env = {**os.environ, "PF_PROJECTS_DIR": str(work / "projects"), "PF_AGENT_ENV": "codex",
               "PYTHONIOENCODING": "utf-8"}
        env.pop("PF_PROJECT", None)
        initial = reuse_plain_phase(previous, out, work, case, "initial", INITIAL) if arm == "plain" else None
        reused_initial = initial is not None
        if initial is None:
            initial = phase(codex, work, out, env, model, arm, case, "initial", INITIAL, timeout)
        revision = None
        if not initial["exit_code"] and initial["answer_file"]:
            shutil.copyfile(HERE / "cases" / case / "update.md", work / "inbox" / "update.md")
            if reuse_revision and reused_initial:
                revision = reuse_plain_phase(previous, out, work, case, "revision", UPDATE)
            if revision is None:
                revision = phase(codex, work, out, env, model, arm, case, "revision", UPDATE, timeout)
        if arm == "pf" and (work / "projects").is_dir():
            shutil.copytree(work / "projects", out / "projects", ignore=shutil.ignore_patterns(".lock"))
        release_code = revision["release_code"] if revision else None
        result = dict(case=case, model=model, arm=arm, initial=initial, revision=revision, release_code=release_code)
        write_json(out / "run.json", result)
    return result


def operational_summary(results):
    summary = []
    for model, arm in sorted({(r["model"], r["arm"]) for r in results}):
        runs = [r for r in results if (r["model"], r["arm"]) == (model, arm)]
        phases = [r[name] for r in runs for name in ("initial", "revision")]
        attempted = [p for p in phases if p]
        usage = [p["usage"] for p in attempted if p["usage"]]
        summary.append({
            "model": model, "arm": arm, "planned_phases": len(phases),
            "outcomes": dict(Counter(p["outcome"] if p else "not_started" for p in phases)),
            "final_reports": sum(bool(r["revision"] and r["revision"]["outcome"] == "published") for r in runs),
            "seconds": round(sum(p["seconds"] for p in attempted), 2),
            "tool_calls": sum(p["tool_calls"] for p in attempted),
            "failed_tools": sum(p["failed_tools"] for p in attempted),
            "phases_with_usage": len(usage),
            "uncached_input_tokens": sum(u["input_tokens"] - u.get("cached_input_tokens", 0) for u in usage),
            "output_tokens": sum(u["output_tokens"] for u in usage),
        })
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path, help="new directory for logs and measurements")
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--model", choices=MODELS, action="append")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=550, help="seconds per model turn")
    reuse = parser.add_mutually_exclusive_group()
    reuse.add_argument("--reuse-plain-initial", type=Path, help="reuse complete baseline starts with identical inputs")
    reuse.add_argument("--reuse-plain-run", type=Path, help="reuse both baseline phases with identical inputs and prompts")
    opts = parser.parse_args()
    if opts.workers < 1 or opts.timeout < 1:
        parser.error("--workers and --timeout must be positive")
    codex = shutil.which("codex")
    if not codex:
        parser.error("Codex CLI must be on PATH")
    cases, models = opts.case or DEFAULT_CASES, opts.model or MODELS
    hashes = {case: {name: file_hash(HERE / "cases" / case / name)
                     for name in ("initial.md", "update.md")} for case in cases}
    previous = opts.reuse_plain_run or opts.reuse_plain_initial
    if previous:
        previous = previous.resolve()
        info = json.loads((previous / "experiment.json").read_text(encoding="utf-8"))
        if info.get("effort") != "low" or any(info["input_sha256"].get(case) != data for case, data in hashes.items()):
            parser.error("the previous run must have the same model effort and input files")
    out = opts.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "experiment.json", {"input_sha256": hashes, "models": models, "effort": "low",
              "evaluation_artifact": "released_report", "timeout_seconds": opts.timeout,
              "reuse_plain": str(previous) if previous else None, "reuse_plain_revision": bool(opts.reuse_plain_run),
              "implementation_sha256": {p: file_hash(ROOT / p) for p in
              ("AGENTS.md", "pf.ps1", "tools/pf.py", "tools/pilot_economics.py",
               "scripts/benchmark/run.py", "scripts/benchmark/serve.py")}})
    jobs = list(itertools.product(cases, models))
    with ThreadPoolExecutor(max_workers=opts.workers) as pool:
        futures = [pool.submit(run_pair_arm, codex, out, case, model, arm, opts.timeout, previous, bool(opts.reuse_plain_run))
                   for index, (case, model) in enumerate(jobs)
                   for arm in (ARMS if index % 2 == 0 else ARMS[::-1])]
        results = [future.result() for future in as_completed(futures)]
    write_json(out / "results.json", sorted(results, key=lambda r: (r["case"], r["model"], r["arm"])))
    write_json(out / "operations.json", operational_summary(results))


if __name__ == "__main__":
    main()
