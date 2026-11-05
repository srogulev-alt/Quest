# -*- coding: utf-8 -*-
"""Сброс пароля администратора: python reset_admin.py НОВЫЙ_ПАРОЛЬ"""
import os, sqlite3, sys
from werkzeug.security import generate_password_hash
if len(sys.argv) != 2 or len(sys.argv[1]) < 6:
    sys.exit("Использование: python reset_admin.py НОВЫЙ_ПАРОЛЬ  (не короче 6 символов)")
base = os.path.dirname(os.path.abspath(__file__))
db = os.path.join(os.environ.get("TEST_DATA_DIR", os.path.join(base, "data")), "test.db")
if not os.path.exists(db):
    sys.exit("База данных не найдена: " + db + ". Сначала запустите сервер.")
con = sqlite3.connect(db)
con.execute("INSERT INTO settings(k,v) VALUES('admin_hash',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (generate_password_hash(sys.argv[1]),))
con.commit()
print("Пароль администратора изменён.")
