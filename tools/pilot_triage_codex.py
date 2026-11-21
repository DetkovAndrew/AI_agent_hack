"""Turn one correction file into a checked preview with one Codex extraction call."""

import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


FIELDS = {"status": {"type": "string", "enum": ["ready", "review"]},
          "terms": {"type": "string"}, "replacement": {"type": "string"},
          "observations": {"type": "string"}, "scenario": {"type": "string"},
          "cohort": {"type": "array", "items": {"type": "integer"}},
          "highlights": {"type": "array", "items": {"type": "string"}}}


def triage(pf, codex, workspace, env, decision_id, source_file, model, timeout=90):
    def project_command(args, *, input=None):
        return subprocess.run([sys.executable, str(pf), *args], cwd=workspace, env=env,
                              input=input, capture_output=True, encoding="utf-8", timeout=30)

    started = time.monotonic()
    ingest = project_command(["ingest", str(source_file)])
    if ingest.returncode:
        raise ValueError(ingest.stderr.strip() or ingest.stdout.strip())
    refs = re.findall(r"(?m)^(S\d+)\t", ingest.stdout)
    if len(refs) != 1:
        raise ValueError("требуется один зарегистрированный файл поправки")
    cmd = ["pilot-triage", "--id", decision_id, "--source", refs[0]]
    prompt = project_command(cmd)
    if prompt.returncode:
        raise ValueError(prompt.stderr.strip() or prompt.stdout.strip())

    with tempfile.TemporaryDirectory(prefix="pf-extract-") as local:
        work = Path(local)
        answer = work / "extraction.json"
        schema = work / "schema.json"
        schema.write_text(json.dumps({"type": "object", "properties": FIELDS, "required": list(FIELDS),
                                      "additionalProperties": False}), encoding="utf-8")
        args = [codex, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
                "--sandbox", "read-only", "--json", "--model", model, "-c", 'model_reasoning_effort="low"',
                "-c", 'web_search="disabled"', "-c", 'approval_policy="never"', "-c", "project_doc_max_bytes=0",
                "--output-schema", str(schema), "--output-last-message", str(answer), "-"]
        run = subprocess.run(args, input=prompt.stdout, cwd=work, env=env, encoding="utf-8",
                             capture_output=True, timeout=timeout)
        events = [json.loads(line) for line in run.stdout.splitlines() if line.strip()]
        if run.returncode or not answer.is_file():
            error = next((line.get("message") for line in reversed(events) if line.get("type") == "error"), None)
            raise ValueError(error or run.stderr.strip() or "Codex не сохранил извлечение")
        proposal = answer.read_text(encoding="utf-8").strip()
        if proposal.startswith('```json\n') and proposal.endswith('```'):
            proposal = proposal[len('```json\n'):-3].strip()
    usage = next((e["usage"] for e in reversed(events) if e["type"] == "turn.completed"), {})
    result = project_command(cmd + ["--proposal", "-"], input=proposal)
    return (result.stdout, result.stderr, result.returncode,
            {"seconds": round(time.monotonic() - started, 2), "model": model, "model_calls": 1,
             "manual_transcriptions": 0, "source": refs[0], "usage": usage,
             "extraction": proposal})
