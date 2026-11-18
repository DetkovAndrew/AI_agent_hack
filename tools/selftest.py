"""Сквозная самопроверка pf.py на временном проекте: python3 tools/selftest.py

Проходит основные команды и проверяет ключевые защиты (факт без источника, неверная цитата,
искажённое число, отзыв и поиск затронутого). Проект удаляется в конце; активный проект
восстанавливается.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SLUG = "_selftest"
ACTIVE = ROOT / "projects" / ".active"
failures = []


def pf(*args, ok=True, expect=None, absent=None):
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "pf.py"), "--project", SLUG, *args],
                       capture_output=True, text=True, encoding="utf-8")
    out = r.stdout + r.stderr
    name = " ".join(args)[:70]
    if ok and r.returncode != 0:
        failures.append(f"{name}: код {r.returncode}\n{out}")
    if not ok and r.returncode == 0:
        failures.append(f"{name}: должно было завершиться ошибкой\n{out}")
    if expect and expect not in out:
        failures.append(f"{name}: нет «{expect}» в выводе\n{out}")
    if absent and absent in out:
        failures.append(f"{name}: лишнее «{absent}» в выводе\n{out}")
    return out


def main():
    prev = ACTIVE.read_text(encoding="utf-8") if ACTIVE.exists() else None
    d = ROOT / "projects" / SLUG
    shutil.rmtree(d, ignore_errors=True)
    try:
        pf("init", SLUG, "--title", "Самопроверка")
        pf("source", "add", "--kind", "note", "--title", "Заметка",
           "--text", "Компания продаёт eSIM для двухсот стран. Цена 599 рублей в месяц.")
        pf("claim", "add", "Факт без источника", "--kind", "fact", ok=False)
        pf("claim", "add", "Цена 599 ₽/мес", "--kind", "fact", "--src", "S001",
           "--quote", "Цена 599 рублей в месяц", expect="C001", absent="ВНИМ")
        pf("claim", "add", "Цена 299 ₽/мес", "--kind", "fact", "--src", "S001",
           "--quote", "Цена 299 рублей в месяц", ok=False, expect="цитата не найдена")
        pf("claim", "add", "Цена 299 ₽/мес", "--kind", "fact", "--src", "S001",
           expect="Чисел 299 нет")
        pf("source", "add", "--kind", "web", "--url", "https://example.invalid/x",
           "--text", "вставленный текст", ok=False, expect="сначала скачайте")
        pf("source", "add", "--kind", "web", "--title", "Сводка поиска", "--text", "цена 777 рублей")
        pf("claim", "add", "Цена 777", "--kind", "fact", "--src", "S002",
           "--quote", "цена 777 рублей", ok=False, expect="сводка поиска без URL")
        pf("claim", "add", "200 стран, 599 ₽", "--kind", "fact", "--src", "S001",
           "--quote", "eSIM для двухсот стран … Цена 599 рублей", absent="нет в цитате")
        pf("claim", "add", "599 ₽", "--kind", "fact", "--src", "S001",
           "--quote", "eSIM для двухсот стран … Цена 499 рублей", ok=False, expect="цитата не найдена")
        pf("source", "add", "--kind", "note", "--title", "Заметка 2",
           "--text", "Полная себестоимость чашки часто достигает 240 рублей.")
        pf("claim", "add", "Себестоимость чашки 240 ₽", "--kind", "fact", "--src", "S003",
           "--quote", "часто достигает 240 рублей", expect="оговорка")
        pf("claim", "add", "Подписку купят 5% клиентов", "--kind", "assumption", "--tags", "G001")
        pf("gap", "add", "Доля покупателей", "--why", "нет данных", "--how", "тест",
           "--impact", "high")
        pf("decision", "add", "--title", "Тест", "--rationale", "r", "--owner", "PM",
           "--basis", "C001,C003", "--metric", "m", "--trigger", "t", "--review-date", "2030-01-01")
        (d / "deliverables").mkdir(exist_ok=True)
        (d / "deliverables" / "summary.md").write_text(
            "Цена [C001]. Спрос 5% — допущение [C003]; пробел [G001].\n", encoding="utf-8")
        pf("check", expect="C002: чисел 299")
        pf("claim", "retract", "C003", "--reason", "тест", expect="отозвано")
        pf("impact", "--claim", "C003", expect="summary.md")
        pf("check", expect="ссылка на отозванное C003")
        pf("decision", "edit", "--id", "D001", "--basis", "C001", "--note", "тест")
        pf("gap", "edit", "G001", "--how", "опрос", "--reason", "тест")
        batch = d / "analysis" / "b.txt"
        batch.write_text("gap list\nclaim show C001\n", encoding="utf-8")
        pf("batch", str(batch), expect="Выполнено команд: 2")
        pf("report")
        pf("status", "--brief")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        if prev is not None:
            ACTIVE.write_text(prev, encoding="utf-8")
    if failures:
        print("САМОПРОВЕРКА НЕ ПРОЙДЕНА:\n\n" + "\n\n".join(failures))
        sys.exit(1)
    print("Самопроверка пройдена.")


if __name__ == "__main__":
    main()
