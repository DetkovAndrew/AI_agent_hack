"""Versioned CSV input for a correction to a saved pilot's control renewals."""

import csv
from decimal import Decimal
from hashlib import sha256
import io
import json
from pathlib import Path
import re

from pilot_triage import GROUPED_INT, renewal_from_cohort


HEADER = ["field", "value"]
CONFIRMATIONS = {
    "same_control_cohort": "нужно подтвердить, что контрольная когорта та же",
    "conditions_unchanged": "нужно подтвердить, что цены, затраты, горизонт и порог прежние",
}
SCENARIO = "control_renewal_pct"
MAX_BYTES = 64 * 1024


def revision_fields(project, decision):
    record = [project, decision["id"], decision["pilot"], decision.get("review_trigger")]
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {"schema": "pilot_control_revision_v1", "decision_id": decision["id"],
            "pilot_revision": sha256(payload).hexdigest()}


def periods(decision):
    return [f"period_{number}" for number in range(decision["pilot"]["terms"]["months"] + 1)]


def write_template(path: Path, project, decision):
    if path.suffix.lower() != ".csv":
        raise ValueError("для шаблона укажите имя с расширением .csv")
    fields = revision_fields(project, decision)
    fields.update(dict.fromkeys([*CONFIRMATIONS, SCENARIO, *periods(decision)], ""))
    with path.open("x", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output, delimiter=";")
        writer.writerow(HEADER)
        writer.writerows(fields.items())


def read_correction(path: Path, project, decision):
    with path.open("rb") as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("стандартная поправка должна занимать не больше 64 КБ")
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError as exc:
        raise ValueError("сохраните файл как CSV UTF-8") from exc

    expected = revision_fields(project, decision)
    period_fields = periods(decision)
    allowed = {*expected, *CONFIRMATIONS, SCENARIO, *period_fields}
    first_line = text.partition("\n")[0]
    delimiter = ";" if ";" in first_line else ","
    rows = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    fields = {}
    try:
        if next(rows, None) != HEADER:
            raise ValueError("ожидается таблица field, value из шаблона --csv-template")
        for row in rows:
            if not row:
                continue
            if len(row) != 2 or "\n" in row[1] or "\r" in row[1]:
                raise ValueError(f"строка {rows.line_num}: нужны имя поля и одно значение")
            name, value = (cell.strip() for cell in row)
            if name not in allowed:
                raise ValueError(f"строка {rows.line_num}: поле {name!r} отсутствует в шаблоне")
            if name in fields:
                raise ValueError(f"строка {rows.line_num}: повтор поля {name}")
            fields[name] = value
    except csv.Error as exc:
        raise ValueError(f"некорректный CSV в строке {rows.line_num}") from exc

    if fields.get("schema") != expected["schema"]:
        raise ValueError("неизвестная версия таблицы. Создайте актуальный шаблон")
    if fields.get("decision_id") != expected["decision_id"]:
        raise ValueError("таблица относится к другому решению")
    if fields.get("pilot_revision") != expected["pilot_revision"]:
        raise ValueError("таблица относится к другой версии пилота или другому проекту. Создайте актуальный шаблон")
    for key, problem in CONFIRMATIONS.items():
        if fields.get(key, "").casefold() not in {"да", "yes"}:
            raise ValueError(f"{key}: {problem}")

    cohort = []
    for key in period_fields:
        value = fields.get(key, "")
        if len(value) > 23 or not GROUPED_INT.fullmatch(value):
            raise ValueError(f"{key}: нужна целая численность по этому периоду")
        cohort.append(int("".join(value.split())))

    value = fields.get(SCENARIO, "")
    if value and not re.fullmatch(r"[0-9]{1,3}(?:[,.][0-9]{1,12})?", value):
        raise ValueError(f"{SCENARIO}: укажите процент числом от 0 до 100")
    declared = Decimal(value.replace(",", ".")) if value else None
    pct = renewal_from_cohort(cohort, declared)
    return raw, text, cohort, pct, declared is not None
