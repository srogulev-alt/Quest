# -*- coding: utf-8 -*-
"""Проверка банка вопросов: python check_bank.py [seed_bank.json] [--strict]

Ошибки (код возврата 1): не 4 разных варианта, неверный индекс ответа, повтор
номера вопроса, неизвестный блок, пустые поля, неверное направление блока.
Предупреждения: правильный ответ самый длинный и заметно длиннее остальных
(по этому признаку ответ можно угадать), совпадающие формулировки вопросов,
вопросы без ссылки на пункт или без пояснения. С --strict предупреждения тоже
дают код возврата 1."""
import json, os, re, sys, collections

HERE = os.path.dirname(os.path.abspath(__file__))
COLS = {"civ", "adm", "bank"}
RATIO_WARN = 1.4          # правильный ответ длиннее среднего неправильного в столько раз


def norm(s):
    return re.sub(r"[\W_]+", " ", s.lower()).strip()


def check(data):
    errors, warns = [], []
    blocks = {b["id"]: b for b in data["blocks"]}
    if len(blocks) != len(data["blocks"]):
        errors.append("повторяются номера блоков")
    for b in data["blocks"]:
        if not str(b.get("title", "")).strip():
            errors.append("блок %s без названия" % b["id"])
        if len(str(b.get("title", ""))) > 300:
            errors.append("блок %s: название длиннее 300 знаков" % b["id"])
        bad = [c for c in b.get("cols", []) if c not in COLS]
        if bad:
            errors.append("блок %s: неизвестные направления %s" % (b["id"], bad))
    seen, texts = set(), collections.defaultdict(list)
    skew = 0
    for q in data["questions"]:
        qid = q.get("id", "?")
        if qid in seen:
            errors.append("повтор номера вопроса %s" % qid)
        seen.add(qid)
        if q.get("b") not in blocks:
            errors.append("%s: нет блока %s" % (qid, q.get("b")))
        o = q.get("o")
        if not (isinstance(o, list) and len(o) == 4 and all(isinstance(x, str) and x.strip() for x in o)):
            errors.append("%s: нужно ровно 4 непустых варианта" % qid)
            continue
        if len({norm(x) for x in o}) != 4:
            errors.append("%s: есть одинаковые варианты" % qid)
        if not (isinstance(q.get("a"), int) and 0 <= q["a"] <= 3):
            errors.append("%s: индекс ответа вне 0..3" % qid)
            continue
        if not str(q.get("q", "")).strip():
            errors.append("%s: пустой вопрос" % qid)
        for f, name in (("act", "название акта"), ("p", "пункт"), ("e", "пояснение")):
            if not str(q.get(f, "")).strip():
                warns.append("%s: нет поля «%s»" % (qid, name))
        c = len(o[q["a"]])
        others = [len(t) for i, t in enumerate(o) if i != q["a"]]
        if c > max(others) and c / (sum(others) / 3) > RATIO_WARN:
            skew += 1
            warns.append("%s: правильный ответ самый длинный (в %.1f раза длиннее среднего)" % (qid, c / (sum(others) / 3)))
        texts[norm(q["q"])].append(qid)
    for t, ids in texts.items():
        if len(ids) > 1:
            warns.append("одинаковая формулировка вопроса: %s" % ", ".join(ids))
    return errors, warns, skew


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else os.path.join(HERE, "seed_bank.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    errors, warns, skew = check(data)
    print("Вопросов: %d, блоков: %d" % (len(data["questions"]), len(data["blocks"])))
    for e in errors:
        print("ОШИБКА:", e)
    shown = 0
    for w in warns:
        if shown < 40:
            print("Предупреждение:", w)
        shown += 1
    if shown > 40:
        print("… и ещё %d предупреждений" % (shown - 40))
    print("Итого: ошибок %d, предупреждений %d (перекос длины: %d)" % (len(errors), len(warns), skew))
    if errors or (warns and "--strict" in sys.argv):
        sys.exit(1)


if __name__ == "__main__":
    main()
