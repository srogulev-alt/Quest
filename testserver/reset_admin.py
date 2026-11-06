# -*- coding: utf-8 -*-
"""Сброс пароля:
  python reset_admin.py НОВЫЙ_ПАРОЛЬ              — пароль администратора
  python reset_admin.py --analyst НОВЫЙ_ПАРОЛЬ    — пароль Отдела анализа
  python reset_admin.py --analyst --off           — закрыть доступ Отдела анализа
После сброса все открытые входы этой роли перестают действовать."""
import os, sqlite3, sys
from werkzeug.security import check_password_hash, generate_password_hash

USAGE = __doc__.split("\n", 1)[1]
args = sys.argv[1:]
role = "admin"
if args[:1] == ["--analyst"]:
    role, args = "analyst", args[1:]
off = role == "analyst" and args == ["--off"]
if not off and (len(args) != 1 or len(args[0]) < 6):
    sys.exit("Использование (пароль не короче 6 символов):\n" + USAGE)
base = os.path.dirname(os.path.abspath(__file__))
db = os.path.join(os.environ.get("TEST_DATA_DIR", os.path.join(base, "data")), "test.db")
if not os.path.exists(db):
    sys.exit("База данных не найдена: " + db + ". Сначала запустите сервер.")
con = sqlite3.connect(db)
if off:
    con.execute("DELETE FROM settings WHERE k='analyst_hash'")
    con.commit()
    sys.exit(print("Доступ Отдела анализа закрыт.") or 0)
other = con.execute("SELECT v FROM settings WHERE k=?", ("analyst_hash" if role == "admin" else "admin_hash",)).fetchone()
if other and check_password_hash(other[0], args[0]):
    sys.exit("Пароли администратора и Отдела анализа не должны совпадать.")
con.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (role + "_hash", generate_password_hash(args[0])))
con.commit()
print("Пароль администратора изменён." if role == "admin" else "Пароль Отдела анализа задан.")
