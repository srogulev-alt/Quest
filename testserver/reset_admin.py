# -*- coding: utf-8 -*-
"""Сброс пароля:
  python reset_admin.py НОВЫЙ_ПАРОЛЬ                 — пароль администратора
  python reset_admin.py --user ЛОГИН НОВЫЙ_ПАРОЛЬ    — пароль пользователя (пользователь также включается)
После сброса все открытые входы с прежним паролем перестают действовать.
Пользователей и их права задаёт администратор в панели: вкладка «Пользователи»."""
import os, sqlite3, sys
from werkzeug.security import generate_password_hash

USAGE = __doc__.split("\n", 1)[1]
args = sys.argv[1:]
login = None
if args[:1] == ["--user"]:
    if len(args) != 3:
        sys.exit("Использование:\n" + USAGE)
    login, args = args[1].strip().casefold(), args[2:]
if len(args) != 1 or len(args[0]) < 6:
    sys.exit("Использование (пароль не короче 6 символов):\n" + USAGE)
base = os.path.dirname(os.path.abspath(__file__))
db = os.path.join(os.environ.get("TEST_DATA_DIR", os.path.join(base, "data")), "test.db")
if not os.path.exists(db):
    sys.exit("База данных не найдена: " + db + ". Сначала запустите сервер.")
con = sqlite3.connect(db)
h = generate_password_hash(args[0])
if login is None:
    con.execute("INSERT INTO settings(k,v) VALUES('admin_hash',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (h,))
    msg = "Пароль администратора изменён."
else:
    try:
        cur = con.execute("UPDATE users SET hash=?, active=1 WHERE login=?", (h, login))
    except sqlite3.OperationalError:
        sys.exit("В базе нет таблицы пользователей: сначала запустите сервер новой версии.")
    if cur.rowcount == 0:
        sys.exit("Пользователь «%s» не найден." % login)
    msg = "Пароль пользователя «%s» изменён." % login
con.commit()
print(msg)
