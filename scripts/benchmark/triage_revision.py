"""Measure file-to-preview for two held-fixed pilot corrections (no human transcription)."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from compact_revision import MODEL as BASELINE_MODEL, normalized_report, replay
from run import HERE, PILOT_CASES, file_hash, prepare, publish_report, write_json

sys.path.insert(0, str(HERE.parents[1] / "tools"))
from pilot_triage_codex import triage


EXPECTED = {
    "cinema": ("Финансовый порог не выполняется", "Контроль — 603,75 → 786,60",
               "Тест при 100% продления — 708,00", "Тест минус контроль — -78,60",
               "96 из 1200", "36 указали", "Репрезентативность не проверялась"),
    "prints": ("Финансовый порог выполняется", "Контроль — 704,57 → 555,96",
               "Тест при 100% продления — 655,00", "Тест минус контроль — 99,04",
               "72 из 1280", "18 отметили", "добровольная выборка"),
}


def measure(out, baseline, case, codex, model):
    archive = baseline / f"{case}.{BASELINE_MODEL}.pf"
    case_dir = out / case
    case_dir.mkdir()
    with tempfile.TemporaryDirectory(prefix="pf-triage-bench-") as tmp:
        work = Path(tmp)
        prepare(work, case, "pf")
        env = {**os.environ, "PF_PROJECTS_DIR": str(work / "projects"), "PF_AGENT_ENV": "codex",
               "PYTHONIOENCODING": "utf-8"}
        env.pop("PF_PROJECT", None)
        replay(work, archive, env)
        outcome, _, _ = publish_report(work, case_dir, env, case, "initial")
        if outcome != "published" or normalized_report(case_dir / "initial.md") != normalized_report(archive / "initial.md"):
            raise ValueError(f"{case}: начальное состояние не совпало с архивом")
        source = HERE / "cases" / case / "update.md"
        project = work / "projects/experiment"
        state_files = [project / path for path in ("decisions/decisions.jsonl", "evidence/claims.jsonl",
                                                     "evidence/verdicts.jsonl", "deliverables/REPORT.md")]
        state_before = {path: file_hash(path) for path in state_files}
        try:
            text, errors, code, metrics = triage(work / "tools/pf.py", codex, work, env, "D001", source, model)
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            text, errors, code, metrics = "", str(exc), 1, {"model": model}
        state_preserved = all(file_hash(path) == sha for path, sha in state_before.items())
        matched = [expected in text for expected in EXPECTED[case]]
        (case_dir / "preview.md").write_text(text, encoding="utf-8")
        (case_dir / "diagnostic.log").write_text(errors, encoding="utf-8")
        plain = json.loads((baseline / f"{case}.{BASELINE_MODEL}.plain/revision.metrics.json").read_text(encoding="utf-8"))
        result = dict(case=case, exit_code=code, checks_passed=sum(matched), checks_total=len(matched),
                      state_preserved=state_preserved, plain_baseline=plain, **metrics)
        write_json(case_dir / "metrics.json", result)
        print(f"{case}: exit={code}, checks={sum(matched)}/{len(matched)}, seconds={metrics.get('seconds')}", flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("--model", default="gpt-6-luna")
    args = parser.parse_args()
    codex = shutil.which("codex")
    if not codex:
        parser.error("Codex CLI must be on PATH")
    previous = args.baseline.resolve()
    manifest = json.loads((previous / "experiment.json").read_text(encoding="utf-8"))
    for case in PILOT_CASES:
        inputs = {name: file_hash(HERE / "cases" / case / name) for name in ("initial.md", "update.md")}
        if manifest["input_sha256"].get(case) != inputs:
            parser.error(f"baseline inputs differ for {case}")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(measure, out, previous, case, codex, args.model) for case in PILOT_CASES]
        result = [job.result() for job in jobs]
    write_json(out / "results.json", result)
    if any(row["exit_code"] or row["checks_passed"] != row["checks_total"] or not row["state_preserved"]
           for row in result):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
