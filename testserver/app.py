# -*- coding: utf-8 -*-
"""Тест по постановлениям Пленумов ВС РФ и ВАС РФ — клиент-серверная версия.

Сервер на Flask, данные в SQLite (файл data/test.db). Правильные ответы
хранятся и проверяются только на сервере: до завершения теста они клиенту
не передаются.
"""
import csv
import hashlib
import hmac
import io
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from functools import wraps

from flask import Flask, Response, g, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("TEST_DATA_DIR", os.path.join(BASE, "data"))
os.makedirs(DATA, exist_ok=True)
DB_PATH = os.path.join(DATA, "test.db")

COLS = {
    "mix": "Микс (все темы)",
    "civ": "Гражданская коллегия",
    "adm": "Административная коллегия",
    "bank": "Банкротная коллегия",
}
DEFAULTS = {
    "title": "Тест по постановлениям Пленумов ВС РФ и ВАС РФ",
    "count": 10,          # вопросов в одной попытке
    "minutes": 10,        # время на попытку
    "max_attempts": 0,    # попыток на одну фамилию (0 — без ограничения)
    "review": "full",     # full — разбор с ответами, score — только баллы
    "cols": ["mix", "civ", "adm", "bank"],
    "blocks": [],         # пусто — все блоки
    "open": True,         # тест открыт для прохождения
}
GRACE = 3                 # секунд на доставку последнего ответа после дедлайна
QID_RE = re.compile(r"^[\w.-]{1,40}$")   # номер вопроса: попадает в адрес /api/admin/questions/<id>
_rnd = random.SystemRandom()


def _secret():
    p = os.path.join(DATA, "secret.key")
    try:
        # O_EXCL: файл создаётся один раз и сразу с правами 0600
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    with open(p) as f:
        return f.read().strip()


app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="/static")
app.config.update(
    SECRET_KEY=_secret(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
)
app.json.ensure_ascii = False
if os.environ.get("TRUST_PROXY") == "1":
    # за обратным прокси (nginx, Caddy): брать IP участника из X-Forwarded-For,
    # иначе блокировка входа и журнал IP работают по адресу прокси
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)


# ----------------------------------------------------------------- база данных
SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks(
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, cols TEXT NOT NULL DEFAULT '["civ","adm","bank"]');
CREATE TABLE IF NOT EXISTS questions(
  id TEXT PRIMARY KEY, block_id INTEGER NOT NULL, q TEXT NOT NULL, opts TEXT NOT NULL,
  ans INTEGER NOT NULL, act TEXT NOT NULL DEFAULT '', pt TEXT NOT NULL DEFAULT '', expl TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL, col TEXT NOT NULL,
  started INTEGER NOT NULL, deadline INTEGER NOT NULL, finished INTEGER, total INTEGER NOT NULL,
  score INTEGER, seconds INTEGER, timeout INTEGER NOT NULL DEFAULT 0, review TEXT NOT NULL, ip TEXT);
CREATE TABLE IF NOT EXISTS items(
  attempt_id TEXT NOT NULL, idx INTEGER NOT NULL, qid TEXT NOT NULL, snap TEXT NOT NULL,
  chosen INTEGER, ok INTEGER,
  PRIMARY KEY(attempt_id, idx), FOREIGN KEY(attempt_id) REFERENCES attempts(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS ix_attempts_name ON attempts(name_key);
CREATE INDEX IF NOT EXISTS ix_items_qid ON items(qid);
CREATE INDEX IF NOT EXISTS ix_attempts_open ON attempts(finished, deadline);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
        g.db.execute("PRAGMA synchronous=NORMAL")  # в режиме WAL безопасно и заметно быстрее
    return g.db


@contextmanager
def tx():
    """Транзакция с блокировкой на запись: проверка и изменение выполняются атомарно."""
    con = db()
    if con.in_transaction:
        con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.rollback()
        raise
    con.commit()


@app.teardown_appcontext
def _close(_exc):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def init_db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    # начальный банк загружается только в новую базу; если администратор позже
    # очистит банк, при перезапуске он не должен «воскреснуть»
    seeded = con.execute("SELECT 1 FROM settings WHERE k='seeded'").fetchone() is not None
    if not seeded and con.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 0:
        seed = os.path.join(BASE, "seed_bank.json")
        if os.path.exists(seed):
            with open(seed, encoding="utf-8") as f:
                data = json.load(f)
            for b in data["blocks"]:
                con.execute("INSERT INTO blocks(id,title,cols) VALUES(?,?,?)",
                            (b["id"], b["title"], json.dumps(b.get("cols") or ["civ", "adm", "bank"])))
            for q in data["questions"]:
                con.execute("INSERT INTO questions(id,block_id,q,opts,ans,act,pt,expl) VALUES(?,?,?,?,?,?,?,?)",
                            (q["id"], q["b"], q["q"], json.dumps(q["o"], ensure_ascii=False), q["a"],
                             q.get("act", ""), q.get("p", ""), q.get("e", "")))
    con.execute("INSERT OR IGNORE INTO settings(k,v) VALUES('seeded','true')")
    env_pw = os.environ.get("TEST_ADMIN_PASSWORD")
    if env_pw and con.execute("SELECT 1 FROM settings WHERE k='admin_hash'").fetchone() is None:
        con.execute("INSERT INTO settings(k,v) VALUES('admin_hash',?)", (generate_password_hash(env_pw),))
    con.commit()
    con.close()


def now():
    return int(time.time())


def jerr(msg, code=400):
    return jsonify({"error": msg}), code


def _s(v):
    """Строка из произвольного значения JSON (None -> '')."""
    return "" if v is None else str(v)


def _int(v, lo=0, hi=2**31 - 1):
    """Целое в границах или None (большие числа иначе роняют запрос к SQLite)."""
    if isinstance(v, bool):
        return None
    try:
        v = int(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if lo <= v <= hi else None


# -------------------------------------------------------------------- настройки
def get_settings():
    s = dict(DEFAULTS)
    for r in db().execute("SELECT k,v FROM settings WHERE k NOT IN ('admin_hash','seeded')"):
        try:
            s[r["k"]] = json.loads(r["v"])
        except ValueError:
            pass
    return s


def put_settings(d):
    out = {}
    title = _s(d.get("title", DEFAULTS["title"])).strip()[:200] or DEFAULTS["title"]
    out["title"] = title
    names = {"count": "Вопросов в одной попытке", "minutes": "Время на попытку",
             "max_attempts": "Попыток на одну фамилию"}
    for key, lo, hi in (("count", 1, 200), ("minutes", 1, 300), ("max_attempts", 0, 100)):
        v = _int(d.get(key, DEFAULTS[key]), lo, hi)
        if v is None:
            raise ValueError("Поле «%s»: целое число от %d до %d" % (names[key], lo, hi))
        out[key] = v
    if d.get("review") not in ("full", "score"):
        raise ValueError("Режим результата: full или score")
    out["review"] = d["review"]
    raw_cols, raw_blocks = d.get("cols"), d.get("blocks") or []
    cols = [c for c in COLS if isinstance(raw_cols, list) and c in raw_cols]
    if not cols:
        raise ValueError("Выберите хотя бы один вариант входа")
    out["cols"] = cols
    blocks = [_int(x) for x in raw_blocks] if isinstance(raw_blocks, list) else [None]
    if None in blocks:
        raise ValueError("Неверный список блоков")
    out["blocks"] = sorted(set(blocks))
    out["open"] = bool(d.get("open", True))
    con = db()
    for k, v in out.items():
        con.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (k, json.dumps(v, ensure_ascii=False)))
    con.commit()
    return out


def _block_ids(col, blocks):
    """Номера блоков, входящих в направление col (с учётом фильтра blocks)."""
    out = []
    for r in db().execute("SELECT id,cols FROM blocks"):
        if blocks and r["id"] not in blocks:
            continue
        if col != "mix" and col not in json.loads(r["cols"]):
            continue
        out.append(r["id"])
    return out


def pool_sizes(blocks):
    """Число вопросов по каждому направлению одним проходом по таблицам."""
    counts = {r[0]: r[1] for r in db().execute("SELECT block_id,COUNT(*) FROM questions GROUP BY block_id")}
    sizes = dict.fromkeys(COLS, 0)
    for r in db().execute("SELECT id,cols FROM blocks"):
        if blocks and r["id"] not in blocks:
            continue
        n = counts.get(r["id"], 0)
        bc = json.loads(r["cols"])
        for c in COLS:
            if c == "mix" or c in bc:
                sizes[c] += n
    return sizes


def _in(n):
    return ",".join("?" * n)


def pool_ids(col, blocks):
    """Номера вопросов направления (без текстов: тексты нужны только выбранным)."""
    ids = _block_ids(col, blocks)
    if not ids:
        return []
    return [r[0] for r in db().execute("SELECT id FROM questions WHERE block_id IN (%s)" % _in(len(ids)), ids)]


# --------------------------------------------------------------------- вход админа
_fails = {}
_fails_lock = threading.Lock()


def admin_hash():
    r = db().execute("SELECT v FROM settings WHERE k='admin_hash'").fetchone()
    return r["v"] if r else None


def _pw_tag(h):
    """Отпечаток текущего пароля в сессии: после смены или сброса пароля
    (в том числе через reset_admin.py) все прежние входы перестают действовать."""
    return hashlib.sha256(h.encode()).hexdigest()[:32] if h else None


def _set_admin(h):
    session.clear()
    session["admin"] = _pw_tag(h)


def is_admin():
    tag = session.get("admin")
    return isinstance(tag, str) and hmac.compare_digest(tag, _pw_tag(admin_hash()) or "")


def _csrf_ok():
    # заголовок нельзя выставить простой формой или ссылкой с чужого сайта
    return request.method in ("GET", "HEAD") or request.headers.get("X-Test-Client") == "1"


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not is_admin():
            return jerr("Требуется вход администратора", 401)
        if not _csrf_ok():
            return jerr("Недопустимый запрос", 400)
        return f(*a, **kw)
    return wrapper


@app.after_request
def _headers(resp):
    # стили можно кешировать с проверкой; страницы и API — никогда
    resp.headers["Cache-Control"] = "no-cache" if request.path.startswith("/static/") else "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["X-Frame-Options"] = "DENY"  # страницы нельзя встроить в чужой сайт (кликджекинг)
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return resp


# ------------------------------------------------------------------- страницы
@app.route("/")
def page_student():
    return send_from_directory(app.static_folder, "student.html")


@app.route("/admin")
def page_admin():
    return send_from_directory(app.static_folder, "admin.html")


# ---------------------------------------------------------------- API проходящего
@app.get("/api/public")
def api_public():
    s = get_settings()
    sizes = pool_sizes(set(s["blocks"]))
    cols = [{"id": c, "title": COLS[c], "n": sizes[c]} for c in COLS if c in s["cols"] and sizes[c]]
    return jsonify({"title": s["title"], "count": s["count"], "minutes": s["minutes"],
                    "open": s["open"], "max_attempts": s["max_attempts"], "cols": cols,
                    "review": s["review"]})


def _norm_name(name):
    return re.sub(r"\s+", " ", name.strip()) if isinstance(name, str) else ""


def _attempt_or_404(aid):
    return db().execute("SELECT * FROM attempts WHERE id=?", (aid,)).fetchone()


def _public_items(aid):
    rows = db().execute("SELECT idx,snap,chosen FROM items WHERE attempt_id=? ORDER BY idx", (aid,)).fetchall()
    out = []
    for r in rows:
        sn = json.loads(r["snap"])
        out.append({"idx": r["idx"], "q": sn["q"], "options": sn["shown"], "chosen": r["chosen"]})
    return out


@app.post("/api/start")
def api_start():
    d = request.get_json(silent=True) or {}
    s = get_settings()
    if not s["open"]:
        return jerr("Тест сейчас закрыт администратором", 403)
    name = _norm_name(d.get("name"))
    if len(name) < 3 or len(name) > 80:
        return jerr("Введите фамилию и имя (от 3 до 80 символов)")
    col = d.get("col")
    if col not in COLS or col not in s["cols"]:
        return jerr("Недопустимый вариант входа")
    key = name.casefold()
    pool = pool_ids(col, set(s["blocks"]))
    if not pool:
        return jerr("В выбранном направлении нет вопросов", 400)
    # случайные вопросы и порядок вариантов; правильный ответ остаётся только на сервере
    picked = _rnd.sample(pool, min(s["count"], len(pool)))
    rows = {r["id"]: r for r in db().execute(
        "SELECT id,q,opts,ans,act,pt,expl FROM questions WHERE id IN (%s)" % _in(len(picked)), picked)}
    chosen = [rows[i] for i in picked if i in rows]  # вопрос мог быть удалён в эту же секунду
    if not chosen:
        return jerr("В выбранном направлении нет вопросов", 400)
    aid = secrets.token_urlsafe(18)
    items, public = [], []
    for i, r in enumerate(chosen):
        opts = json.loads(r["opts"])
        order = list(range(4))
        _rnd.shuffle(order)
        shown = [opts[k] for k in order]
        snap = {"q": r["q"], "shown": shown, "correct": order.index(r["ans"]),
                "expl": r["expl"], "act": r["act"], "pt": r["pt"]}
        items.append((aid, i, r["id"], json.dumps(snap, ensure_ascii=False)))
        public.append({"idx": i, "q": r["q"], "options": shown, "chosen": None})
    # подсчёт попыток и запись — в одной транзакции, чтобы одновременные
    # запросы под одной фамилией не обходили лимит
    with tx() as con:
        if s["max_attempts"]:
            used = con.execute("SELECT COUNT(*) FROM attempts WHERE name_key=?", (key,)).fetchone()[0]
            if used >= s["max_attempts"]:
                return jerr("Для этой фамилии исчерпан лимит попыток (%d)" % s["max_attempts"], 403)
        t = now()
        con.execute("INSERT INTO attempts(id,name,name_key,col,started,deadline,total,review,ip) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (aid, name, key, col, t, t + s["minutes"] * 60, len(chosen), s["review"], request.remote_addr))
        con.executemany("INSERT INTO items(attempt_id,idx,qid,snap) VALUES(?,?,?,?)", items)
    return jsonify({"attempt_id": aid, "total": len(chosen), "remaining": s["minutes"] * 60,
                    "name": name, "questions": public})


def _finalize(a):
    """Проверка ответов и закрытие попытки. Идемпотентна и безопасна при
    одновременных вызовах: всё выполняется в одной транзакции с блокировкой."""
    if a["finished"]:
        return
    with tx() as con:
        a = con.execute("SELECT * FROM attempts WHERE id=?", (a["id"],)).fetchone()
        if a is None or a["finished"]:
            return
        t = now()
        score, oks = 0, []
        for it in con.execute("SELECT idx,snap,chosen FROM items WHERE attempt_id=?", (a["id"],)).fetchall():
            ok = 1 if it["chosen"] is not None and it["chosen"] == json.loads(it["snap"])["correct"] else 0
            score += ok
            oks.append((ok, a["id"], it["idx"]))
        con.executemany("UPDATE items SET ok=? WHERE attempt_id=? AND idx=?", oks)
        # попытка, закрытая позже дедлайна (браузер закрыли, ответы не сохранялись),
        # получает время закрытия = дедлайну
        finished = min(t, a["deadline"])
        con.execute("UPDATE attempts SET finished=?,score=?,seconds=?,timeout=? WHERE id=?",
                    (finished, score, max(0, finished - a["started"]), 1 if t >= a["deadline"] else 0, a["id"]))


def _finalize_expired():
    """Закрыть попытки, брошенные участниками после истечения времени."""
    rows = db().execute("SELECT * FROM attempts WHERE finished IS NULL AND deadline<?", (now() - GRACE,)).fetchall()
    for a in rows:
        _finalize(a)


def _result(aid):
    a = _attempt_or_404(aid)
    out = {"finished": True, "name": a["name"], "col": a["col"], "col_title": COLS.get(a["col"], a["col"]),
           "total": a["total"], "score": a["score"], "seconds": a["seconds"], "timeout": bool(a["timeout"]),
           "review": a["review"]}
    rows = db().execute("SELECT idx,snap,chosen,ok FROM items WHERE attempt_id=? ORDER BY idx", (aid,)).fetchall()
    if a["review"] == "full":
        out["items"] = []
        for r in rows:
            sn = json.loads(r["snap"])
            out["items"].append({"idx": r["idx"], "q": sn["q"], "options": sn["shown"], "chosen": r["chosen"],
                                 "correct": sn["correct"], "ok": bool(r["ok"]), "expl": sn["expl"],
                                 "act": sn["act"], "pt": sn["pt"]})
    else:
        out["wrong"] = [r["idx"] + 1 for r in rows if not r["ok"]]
    return out


@app.get("/api/attempt/<aid>")
def api_attempt(aid):
    a = _attempt_or_404(aid)
    if not a:
        return jerr("Попытка не найдена", 404)
    t = now()
    if not a["finished"] and t > a["deadline"] + GRACE:
        _finalize(a)
        a = _attempt_or_404(aid)
    if a["finished"]:
        return jsonify(_result(aid))
    return jsonify({"finished": False, "attempt_id": aid, "name": a["name"], "total": a["total"],
                    "remaining": max(0, a["deadline"] - t), "questions": _public_items(aid)})


@app.post("/api/attempt/<aid>/answer")
def api_answer(aid):
    a = _attempt_or_404(aid)
    if not a:
        return jerr("Попытка не найдена", 404)
    if a["finished"]:
        return jsonify({"finished": True}), 409
    t = now()
    if t > a["deadline"] + GRACE:
        _finalize(a)
        return jsonify({"finished": True}), 409
    d = request.get_json(silent=True) or {}
    idx = _int(d.get("idx"), 0, 999)
    choice = None if d.get("choice") is None else _int(d.get("choice"), 0, 3)
    if idx is None or (choice is None and d.get("choice") is not None):
        return jerr("Неверные данные")
    # условие на попытку в том же запросе: ответ не попадёт в уже закрытую попытку
    cur = db().execute("UPDATE items SET chosen=? WHERE attempt_id=? AND idx=? AND EXISTS("
                       "SELECT 1 FROM attempts WHERE id=? AND finished IS NULL)", (choice, aid, idx, aid))
    db().commit()
    if cur.rowcount == 0:
        if _attempt_or_404(aid)["finished"]:
            return jsonify({"finished": True}), 409
        return jerr("Вопрос не найден", 404)
    return jsonify({"ok": True, "remaining": max(0, a["deadline"] - t)})


@app.post("/api/attempt/<aid>/finish")
def api_finish(aid):
    a = _attempt_or_404(aid)
    if not a:
        return jerr("Попытка не найдена", 404)
    _finalize(a)
    return jsonify(_result(aid))


# ------------------------------------------------------------------ API админа
@app.get("/api/admin/me")
def admin_me():
    return jsonify({"configured": admin_hash() is not None, "authed": is_admin()})


@app.post("/api/admin/setup")
def admin_setup():
    if not _csrf_ok():
        return jerr("Недопустимый запрос", 400)
    if admin_hash() is not None:
        return jerr("Пароль уже задан", 403)
    pw = _s((request.get_json(silent=True) or {}).get("password"))
    if len(pw) < 6:
        return jerr("Пароль не короче 6 символов")
    # INSERT OR IGNORE + rowcount: два одновременных запроса не перезапишут пароль друг друга
    h = generate_password_hash(pw)
    cur = db().execute("INSERT OR IGNORE INTO settings(k,v) VALUES('admin_hash',?)", (h,))
    db().commit()
    if cur.rowcount == 0:
        return jerr("Пароль уже задан", 403)
    _set_admin(h)
    return jsonify({"ok": True})


@app.post("/api/admin/login")
def admin_login():
    if not _csrf_ok():
        return jerr("Недопустимый запрос", 400)
    ip = request.remote_addr or "?"
    with _fails_lock:
        n, until = _fails.get(ip, (0, 0))
        if until > time.time():
            return jerr("Слишком много попыток. Повторите через минуту", 429)
    h = admin_hash()
    pw = _s((request.get_json(silent=True) or {}).get("password"))
    if h and check_password_hash(h, pw):
        with _fails_lock:
            _fails.pop(ip, None)
        _set_admin(h)
        return jsonify({"ok": True})
    with _fails_lock:
        n, _ = _fails.get(ip, (0, 0))  # перечитать: счётчик мог измениться параллельным запросом
        n += 1
        _fails[ip] = (n, time.time() + 60 if n >= 5 else 0)
        if len(_fails) > 10000:  # не копить записи бесконечно
            _fails.clear()
    return jerr("Неверный пароль", 401)


@app.post("/api/admin/logout")
def admin_logout():
    if not _csrf_ok():
        return jerr("Недопустимый запрос", 400)
    session.clear()
    return jsonify({"ok": True})


@app.post("/api/admin/password")
@admin_required
def admin_password():
    d = request.get_json(silent=True) or {}
    old, new = _s(d.get("old")), _s(d.get("new"))
    if not check_password_hash(admin_hash(), old):
        return jerr("Текущий пароль неверный", 403)
    if len(new) < 6:
        return jerr("Новый пароль не короче 6 символов")
    h = generate_password_hash(new)
    db().execute("UPDATE settings SET v=? WHERE k='admin_hash'", (h,))
    db().commit()
    _set_admin(h)  # этот вход остаётся, остальные сессии закрываются
    return jsonify({"ok": True})


@app.get("/api/admin/settings")
@admin_required
def admin_get_settings():
    return jsonify({"settings": get_settings(), "cols": COLS})


@app.put("/api/admin/settings")
@admin_required
def admin_put_settings():
    try:
        return jsonify({"settings": put_settings(request.get_json(silent=True) or {})})
    except ValueError as e:
        return jerr(str(e))


# --- блоки
def _block_json(r, counts):
    return {"id": r["id"], "title": r["title"], "cols": json.loads(r["cols"]), "n": counts.get(r["id"], 0)}


@app.get("/api/admin/blocks")
@admin_required
def admin_blocks():
    counts = {r[0]: r[1] for r in db().execute("SELECT block_id,COUNT(*) FROM questions GROUP BY block_id")}
    rows = db().execute("SELECT * FROM blocks ORDER BY id").fetchall()
    return jsonify({"blocks": [_block_json(r, counts) for r in rows], "totals": pool_sizes(set())})


def _clean_cols(v):
    return [c for c in ("civ", "adm", "bank") if isinstance(v, list) and c in v]


@app.post("/api/admin/blocks")
@admin_required
def admin_block_create():
    d = request.get_json(silent=True) or {}
    title = _s(d.get("title")).strip()
    if not title:
        return jerr("Введите название блока")
    nid = (db().execute("SELECT COALESCE(MAX(id),0) FROM blocks").fetchone()[0]) + 1
    db().execute("INSERT INTO blocks(id,title,cols) VALUES(?,?,?)", (nid, title[:300], json.dumps(_clean_cols(d.get("cols")))))
    db().commit()
    return jsonify({"id": nid})


@app.put("/api/admin/blocks/<int:bid>")
@admin_required
def admin_block_update(bid):
    d = request.get_json(silent=True) or {}
    r = db().execute("SELECT * FROM blocks WHERE id=?", (bid,)).fetchone()
    if not r:
        return jerr("Блок не найден", 404)
    title = _s(d.get("title")).strip() or r["title"]
    cols = _clean_cols(d["cols"]) if "cols" in d else json.loads(r["cols"])
    db().execute("UPDATE blocks SET title=?,cols=? WHERE id=?", (title[:300], json.dumps(cols), bid))
    db().commit()
    return jsonify({"ok": True})


@app.delete("/api/admin/blocks/<int:bid>")
@admin_required
def admin_block_delete(bid):
    if db().execute("SELECT 1 FROM questions WHERE block_id=? LIMIT 1", (bid,)).fetchone():
        return jerr("В блоке есть вопросы: сначала удалите или перенесите их", 409)
    with tx() as con:
        con.execute("DELETE FROM blocks WHERE id=?", (bid,))
        _prune_block_filter(con)
    return jsonify({"ok": True})


def _prune_block_filter(con):
    """Убрать из настроек удалённые блоки. Иначе, если были отмечены только они,
    фильтр перестаёт совпадать с чем-либо и тест молча остаётся без вопросов."""
    r = con.execute("SELECT v FROM settings WHERE k='blocks'").fetchone()
    if not r:
        return
    sel = json.loads(r["v"])
    alive = {x[0] for x in con.execute("SELECT id FROM blocks")}
    kept = [b for b in sel if b in alive]
    if kept != sel:
        con.execute("UPDATE settings SET v=? WHERE k='blocks'", (json.dumps(kept),))


# --- вопросы
def _q_json(r):
    return {"id": r["id"], "b": r["block_id"], "q": r["q"], "o": json.loads(r["opts"]), "a": r["ans"],
            "act": r["act"], "p": r["pt"], "e": r["expl"]}


def _clean_question(d):
    q = _s(d.get("q")).strip()
    o = [_s(x).strip() for x in d.get("o")] if isinstance(d.get("o"), list) else []
    errs = []
    if not q:
        errs.append("введите текст вопроса")
    if len(o) != 4 or any(not x for x in o):
        errs.append("заполните все четыре варианта")
    elif len(set(o)) < 4:
        errs.append("варианты не должны повторяться")
    a = _int(d.get("a"), 0, 3)
    if a is None:
        errs.append("отметьте правильный вариант")
        a = 0
    b = _int(d.get("b"))
    if b is None or not db().execute("SELECT 1 FROM blocks WHERE id=?", (b,)).fetchone():
        errs.append("выберите блок")
    return errs, {"q": q, "o": o, "a": a, "b": b, "act": _s(d.get("act")).strip(),
                  "p": _s(d.get("p")).strip(), "e": _s(d.get("e")).strip()}


@app.get("/api/admin/questions")
@admin_required
def admin_questions():
    block = request.args.get("block", type=int)
    text = (request.args.get("q") or "").strip().lower()
    offset = max(0, request.args.get("offset", 0, type=int))
    limit = min(500, max(1, request.args.get("limit", 100, type=int)))
    rows = db().execute("SELECT * FROM questions" + (" WHERE block_id=?" if block else "") +
                        " ORDER BY block_id, id", (block,) if block else ()).fetchall()
    items = [_q_json(r) for r in rows]
    if text:
        items = [x for x in items if text in (x["q"] + " " + " ".join(x["o"]) + " " + x["act"] + " " + x["p"] + " " + x["id"]).lower()]
    items.sort(key=lambda x: (x["b"], _qnum(x["id"]), x["id"]))
    return jsonify({"total": len(items), "items": items[offset:offset + limit]})


def _qnum(qid):
    """Номер вопроса внутри блока из id вида «12-07» (для естественной сортировки)."""
    tail = qid.split("-")[-1]
    return int(tail) if tail.isdigit() else 0


@app.get("/api/admin/questions/<qid>")
@admin_required
def admin_question(qid):
    r = db().execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    return jsonify(_q_json(r)) if r else jerr("Вопрос не найден", 404)


@app.post("/api/admin/questions")
@admin_required
def admin_question_create():
    d = request.get_json(silent=True) or {}
    errs, c = _clean_question(d)
    if errs:
        return jerr("Не сохранено: " + "; ".join(errs) + ".")
    n = max([_qnum(r[0]) for r in db().execute("SELECT id FROM questions WHERE block_id=?", (c["b"],))],
            default=0) + 1
    qid = "%d-%02d" % (c["b"], n)
    while db().execute("SELECT 1 FROM questions WHERE id=?", (qid,)).fetchone():
        n += 1
        qid = "%d-%02d" % (c["b"], n)
    db().execute("INSERT INTO questions(id,block_id,q,opts,ans,act,pt,expl) VALUES(?,?,?,?,?,?,?,?)",
                 (qid, c["b"], c["q"], json.dumps(c["o"], ensure_ascii=False), c["a"], c["act"], c["p"], c["e"]))
    db().commit()
    return jsonify({"id": qid})


@app.put("/api/admin/questions/<qid>")
@admin_required
def admin_question_update(qid):
    if not db().execute("SELECT 1 FROM questions WHERE id=?", (qid,)).fetchone():
        return jerr("Вопрос не найден", 404)
    errs, c = _clean_question(request.get_json(silent=True) or {})
    if errs:
        return jerr("Не сохранено: " + "; ".join(errs) + ".")
    db().execute("UPDATE questions SET block_id=?,q=?,opts=?,ans=?,act=?,pt=?,expl=? WHERE id=?",
                 (c["b"], c["q"], json.dumps(c["o"], ensure_ascii=False), c["a"], c["act"], c["p"], c["e"], qid))
    db().commit()
    return jsonify({"ok": True})


@app.delete("/api/admin/questions/<qid>")
@admin_required
def admin_question_delete(qid):
    db().execute("DELETE FROM questions WHERE id=?", (qid,))
    db().commit()
    return jsonify({"ok": True})


# --- экспорт / импорт банка
@app.get("/api/admin/export")
@admin_required
def admin_export():
    blocks = [{"id": r["id"], "title": r["title"], "cols": json.loads(r["cols"])}
              for r in db().execute("SELECT * FROM blocks ORDER BY id")]
    qs = [_q_json(r) for r in db().execute("SELECT * FROM questions ORDER BY block_id,id")]
    body = json.dumps({"version": 1, "blocks": blocks, "questions": qs}, ensure_ascii=False, indent=1)
    return Response(body, mimetype="application/json; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=voprosy-plenumy.json"})


@app.post("/api/admin/import")
@admin_required
def admin_import():
    mode = request.args.get("mode", "merge")
    if mode not in ("merge", "replace"):
        return jerr("Неизвестный режим импорта")
    d = request.get_json(silent=True)
    if not isinstance(d, dict) or not isinstance(d.get("blocks"), list) or not isinstance(d.get("questions"), list):
        return jerr("Нужен JSON, выгруженный кнопкой «Экспорт»")
    # сначала полная проверка и нормализация, затем запись одной транзакцией
    blocks = []
    for b in d["blocks"]:
        bid = _int(b.get("id")) if isinstance(b, dict) else None
        if bid is None:
            return jerr("Неверная запись блока: %s" % (b.get("id") if isinstance(b, dict) else "?"))
        title = _s(b.get("title") or b.get("t")).strip()[:300] or "Блок %d" % bid
        blocks.append((bid, title, json.dumps(_clean_cols(b.get("cols") if "cols" in b else b.get("col")))))
    qs = []
    for q in d["questions"]:
        if not isinstance(q, dict):
            return jerr("Неверная запись вопроса: ?")
        qid, bid, opts = _s(q.get("id")).strip(), _int(q.get("b")), q.get("o")
        if not (QID_RE.match(qid) and _s(q.get("q")).strip() and bid is not None and isinstance(opts, list)
                and len(opts) == 4 and all(_s(x).strip() for x in opts) and type(q.get("a")) is int
                and 0 <= q["a"] <= 3):
            return jerr("Неверная запись вопроса: %s" % (qid or "?"))
        qs.append((qid, bid, _s(q["q"]).strip(), json.dumps([_s(x).strip() for x in opts], ensure_ascii=False),
                   q["a"], _s(q.get("act")).strip(), _s(q.get("p")).strip(), _s(q.get("e")).strip()))
    if len({x[0] for x in qs}) != len(qs):
        return jerr("В файле повторяются номера вопросов")
    added = updated = 0
    with tx() as con:
        if mode == "replace":
            con.execute("DELETE FROM questions")
            con.execute("DELETE FROM blocks")
        if request.args.get("blocks") == "update":
            # обновить названия и направления уже существующих блоков (по умолчанию они сохраняются)
            con.executemany("INSERT INTO blocks(id,title,cols) VALUES(?,?,?) "
                            "ON CONFLICT(id) DO UPDATE SET title=excluded.title,cols=excluded.cols", blocks)
        else:
            con.executemany("INSERT INTO blocks(id,title,cols) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING", blocks)
        for row in qs:
            con.execute("INSERT INTO blocks(id,title,cols) VALUES(?,?,'[]') ON CONFLICT(id) DO NOTHING",
                        (row[1], "Блок %d" % row[1]))
            exists = con.execute("SELECT 1 FROM questions WHERE id=?", (row[0],)).fetchone() is not None
            con.execute("INSERT INTO questions(id,block_id,q,opts,ans,act,pt,expl) VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET block_id=excluded.block_id,q=excluded.q,opts=excluded.opts,"
                        "ans=excluded.ans,act=excluded.act,pt=excluded.pt,expl=excluded.expl", row)
            updated += exists
            added += not exists
        _prune_block_filter(con)
    return jsonify({"added": added, "updated": updated})


# --- результаты
def _attempt_row(r):
    return {"id": r["id"], "name": r["name"], "col": r["col"], "col_title": COLS.get(r["col"], r["col"]),
            "started": r["started"], "finished": r["finished"], "total": r["total"], "score": r["score"],
            "seconds": r["seconds"], "timeout": bool(r["timeout"])}


@app.get("/api/admin/attempts")
@admin_required
def admin_attempts():
    _finalize_expired()
    name = _norm_name(request.args.get("name")).casefold()
    col = request.args.get("col")
    sql, args = "SELECT * FROM attempts", ()
    if col:
        sql, args = sql + " WHERE col=?", (col,)
    rows = db().execute(sql + " ORDER BY started DESC", args).fetchall()
    out = [_attempt_row(r) for r in rows if not name or name in r["name_key"]]
    return jsonify({"total": len(out), "items": out[:1000]})


@app.get("/api/admin/attempts/<aid>")
@admin_required
def admin_attempt(aid):
    a = _attempt_or_404(aid)
    if not a:
        return jerr("Попытка не найдена", 404)
    if not a["finished"] and a["deadline"] + GRACE < now():
        _finalize(a)
        a = _attempt_or_404(aid)
    items = []
    for r in db().execute("SELECT idx,qid,snap,chosen,ok FROM items WHERE attempt_id=? ORDER BY idx", (aid,)):
        sn = json.loads(r["snap"])
        items.append({"idx": r["idx"], "qid": r["qid"], "q": sn["q"], "options": sn["shown"], "chosen": r["chosen"],
                      "correct": sn["correct"], "ok": None if r["ok"] is None else bool(r["ok"])})
    return jsonify({"attempt": _attempt_row(a), "items": items})


@app.delete("/api/admin/attempts/<aid>")
@admin_required
def admin_attempt_delete(aid):
    db().execute("DELETE FROM attempts WHERE id=?", (aid,))
    db().commit()
    return jsonify({"ok": True})


@app.get("/api/admin/stats")
@admin_required
def admin_stats():
    _finalize_expired()
    rows = db().execute(
        "SELECT s.qid, s.n, s.bad, q.q FROM ("
        " SELECT i.qid, COUNT(*) n, SUM(CASE WHEN i.ok=0 THEN 1 ELSE 0 END) bad FROM items i"
        " JOIN attempts a ON a.id=i.attempt_id WHERE a.finished IS NOT NULL GROUP BY i.qid"
        " HAVING bad>0 ORDER BY (bad*1.0/n) DESC, n DESC LIMIT 15) s"
        " LEFT JOIN questions q ON q.id=s.qid ORDER BY (s.bad*1.0/s.n) DESC, s.n DESC").fetchall()
    out = [{"qid": r["qid"], "q": r["q"] if r["q"] is not None else "(вопрос удалён)", "n": r["n"], "bad": r["bad"]}
           for r in rows]
    done = db().execute("SELECT COUNT(*), AVG(score*100.0/total) FROM attempts WHERE finished IS NOT NULL").fetchone()
    return jsonify({"hard": out, "attempts": done[0], "avg": round(done[1], 1) if done[1] is not None else None})


def _csv_safe(v):
    """Защита от формул в Excel: фамилию участник вводит сам (например «=HYPERLINK(...)»)."""
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


@app.get("/api/admin/results.csv")
@admin_required
def admin_csv():
    _finalize_expired()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_ALL)
    w.writerow(["Участник", "Направление", "Начало", "Верно", "Всего", "Процент", "Секунд", "Время вышло", "Статус"])
    for r in db().execute("SELECT * FROM attempts ORDER BY started DESC"):
        done = r["finished"] is not None
        w.writerow([_csv_safe(r["name"]), COLS.get(r["col"], r["col"]), time.strftime("%d.%m.%Y %H:%M", time.localtime(r["started"])),
                    r["score"] if done else "", r["total"], round(r["score"] * 100 / r["total"]) if done else "",
                    r["seconds"] if done else "", "да" if r["timeout"] else "нет", "завершён" if done else "не завершён"])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=rezultaty.csv"})


@app.errorhandler(OverflowError)
def _overflow(_e):
    return jerr("Слишком большое число в запросе")


init_db()

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    print("Сервер теста: http://%s:%d  (администратор: /admin)" % ("localhost" if host == "0.0.0.0" else host, port))
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, threaded=True)
