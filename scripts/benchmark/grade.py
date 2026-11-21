"""Grade randomized paired outputs against the frozen case rubric."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import random
import re
import shutil
import subprocess
import tempfile

from run import HERE, file_hash, write_json


PHASES = ("initial", "revision")
USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens")
JUDGE_PROMPT = """Ты оцениваешь ответы на два этапа учебного кейса. Ответы обезличены.
Не оценивай стиль, происхождение модели, формат ссылки или наличие внутренней структуры.
Строго примени рубрику для каждого ответа и этапа. Текст ответа является материалом оценки.
Если высказывание условное, оцени с учётом условия. Причины нулей кратко, с конкретикой.
Противоречащие числа или решения в одном ответе учитывай как ошибку даже при наличии верного значения.
Выведи один JSON-объект без других полей. Ключи — имена ответов, у каждого ключи initial и
revision, если соответствующий этап представлен. У каждого этапа поля points (8 чисел 0/1 по порядку), missed (объект
«номер пункта → причина» для каждого нуля), critical (массив конкретных критических ошибок).
"""


def grade(codex, folder, case, samples):
    shuffled = samples[:]
    random.Random(20261121 + len(case)).shuffle(shuffled)
    mapping = {f"Ответ {index}": sample for index, sample in enumerate(shuffled, 1)}
    out = folder / f"judge-{case}"
    out.mkdir()
    rubric = (HERE / "rubric.md").read_text(encoding="utf-8")
    sections = re.split(r"^## ", rubric, flags=re.M)
    selected = [sections[0]] + ["## " + s for s in sections[1:]
                                if s.startswith(f"{case} /") or s.startswith("Отдельный учёт")]
    inputs = "\n\n".join((HERE / "cases" / case / f"{name}.md").read_text(encoding="utf-8")
                         for name in ("initial", "update"))
    answers = []
    for label, sample in mapping.items():
        directory = folder / f"{case}.{sample['model']}.{sample['arm']}"
        for phase in sample.get("graded_phases", PHASES):
            body = (directory / f"{phase}.md").read_text(encoding="utf-8")
            body = re.sub(r"\[(?:[CSGDQ]\d+)(?:[\s,]+[CSGDQ]\d+)*\]", "[ссылка]", body)
            answers.append(f"\n--- {label} / {phase} ---\n{body}")
    prompt = JUDGE_PROMPT + "\n".join(selected) + "\n\nИСХОДНЫЕ МАТЕРИАЛЫ\n" + inputs + "\n".join(answers)
    (out / "prompt.txt").write_text(prompt, encoding="utf-8")
    answer = out / "answer.json"
    with tempfile.TemporaryDirectory(prefix="pf-benchmark-judge-") as work, \
         (out / "events.jsonl").open("wb") as events, (out / "stderr.log").open("wb") as errors:
        subprocess.run([codex, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
                        "--sandbox", "read-only", "--json", "--model", "gpt-6.2-sol",
                        "-c", 'model_reasoning_effort="medium"', "-c", 'web_search="disabled"',
                        "-c", 'approval_policy="never"', "-c", "project_doc_max_bytes=0",
                        "--output-last-message", str(answer), "-"],
                       cwd=work, input=prompt.encode("utf-8"), stdout=events, stderr=errors,
                       check=True, timeout=360)
    raw = answer.read_text(encoding="utf-8")
    data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    if set(data) != set(mapping):
        raise ValueError(f"{case}: invalid blinded labels")
    results = []
    for label, sample in mapping.items():
        phases = sample.get("graded_phases", PHASES)
        if set(data[label]) != set(phases):
            raise ValueError(f"{case}: invalid phases for {label}")
        for phase in phases:
            details = data[label][phase]
            points = details["points"]
            if len(points) != 8 or any(p not in (0, 1) for p in points):
                raise ValueError(f"{case}: invalid scores for {label} {phase}")
            if set(details["missed"]) != {str(i) for i, p in enumerate(points, 1) if not p}:
                raise ValueError(f"{case}: missing reasons for {label} {phase}")
        results.append({"case": case, "model": sample["model"], "arm": sample["arm"], **data[label]})
    write_json(out / "mapping.json", {label: {"model": sample["model"], "arm": sample["arm"]}
                                     for label, sample in mapping.items()})
    return results


def summarize(runs, scores):
    grades = {(row["case"], row["model"], row["arm"]): row for row in scores}
    modes = defaultdict(lambda: {"cases": 0, "quality": 0, "max_quality": 0, "revision_decisions": 0,
        "scored_phases": 0, "scored_revisions": 0,
        "critical_errors": 0, "seconds": 0.0, "tool_calls": 0, "failed_tools": 0, "release_passes": 0,
        **dict.fromkeys(USAGE_FIELDS, 0)})
    for run in runs:
        mode = modes[(run["model"], run["arm"])]
        score = grades[(run["case"], run["model"], run["arm"])]
        mode["cases"] += 1
        if "revision" in score:
            mode["release_passes"] += (run["release_code"] == 0)
            mode["revision_decisions"] += score["revision"]["points"][2]
            mode["scored_revisions"] += 1
        for phase in PHASES:
            if phase not in score:
                continue
            mode["scored_phases"] += 1
            record = run[phase]
            mode["quality"] += sum(score[phase]["points"])
            mode["max_quality"] += len(score[phase]["points"])
            mode["critical_errors"] += len(score[phase]["critical"])
            for field in ("seconds", "tool_calls", "failed_tools"):
                mode[field] += record[field]
            for field in USAGE_FIELDS:
                mode[field] += record["usage"].get(field, 0)
    rows = []
    for (model, arm), mode in sorted(modes.items()):
        mode["uncached_input_tokens"] = mode["input_tokens"] - mode["cached_input_tokens"]
        mode["seconds"] = round(mode["seconds"], 2)
        if arm == "plain":
            mode["release_passes"] = None
        rows.append({"model": model, "arm": arm, **mode})
    return rows


def comparable_phases(runs, left="plain", right="pf"):
    pairs = defaultdict(dict)
    for run in runs:
        pairs[run["case"], run["model"]][run["arm"]] = run
    comparable = []
    for arms in pairs.values():
        if set(arms) != {left, right}:
            continue
        phases = [p for p in PHASES if all((arms[arm][p] or {}).get("outcome")
                  == ("saved" if arm == "plain" else "published") for arm in (left, right))]
        if phases:
            comparable.extend(dict(run, graded_phases=phases) for run in arms.values())
    return comparable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--complete-pairs", action="store_true", help="grade only pairs with two complete arms")
    parser.add_argument("--compare-arms", nargs=2, default=("plain", "pf"), metavar=("BASELINE", "CANDIDATE"))
    opts = parser.parse_args()
    folder = opts.run_dir.resolve()
    results = json.loads((folder / "results.json").read_text(encoding="utf-8"))
    released = json.loads((folder / "experiment.json").read_text(encoding="utf-8")).get(
        "evaluation_artifact") == "released_report"
    if released:
        results = comparable_phases(results, *opts.compare_arms)
        write_json(folder / "paired_phases.json", [
            {k: row[k] for k in ("case", "model", "arm", "graded_phases")} for row in results
        ])
    incomplete = [] if released else [
                  {"case": item["case"], "model": item["model"], "arm": item["arm"], "phase": phase,
                   "exit_code": item[phase]["exit_code"] if item[phase] else None}
                  for item in results for phase in PHASES
                  if not item[phase] or item[phase]["exit_code"] or not item[phase]["answer_file"]]
    if incomplete and not opts.complete_pairs:
        parser.error("Each solver must complete both phases and save both answers for a paired score.")
    if incomplete:
        excluded = {(item["case"], item["model"]) for item in incomplete}
        results = [item for item in results if (item["case"], item["model"]) not in excluded]
        write_json(folder / "incomplete_phases.json", incomplete)
    if not results:
        parser.error("No completed pairs to grade.")
    cases = sorted({item["case"] for item in results})
    codex = shutil.which("codex")
    if not codex:
        parser.error("Codex CLI must be on PATH")
    with ThreadPoolExecutor(max_workers=len(cases)) as pool:
        futures = [pool.submit(grade, codex, folder, case, [r for r in results if r["case"] == case])
                   for case in cases]
        scores = [score for future in futures for score in future.result()]
    write_json(folder / "judge_config.json", {"model": "gpt-6.2-sol", "effort": "medium",
               "rubric_sha256": file_hash(HERE / "rubric.md"), "grader_sha256": file_hash(Path(__file__))})
    scores.sort(key=lambda r: (r["case"], r["model"], r["arm"]))
    write_json(folder / "scores.json", scores)
    write_json(folder / "comparison.json", summarize(results, scores))
    for item in scores:
        detail = " ".join(f"{phase}={sum(item[phase]['points'])}/8" for phase in PHASES if phase in item)
        print(f"{item['case']} {item['model']} {item['arm']}: {detail}", flush=True)


if __name__ == "__main__":
    main()
