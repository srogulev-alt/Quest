# -*- coding: utf-8 -*-
"""Резервная копия базы (можно выполнять при работающем сервере): python backup.py"""
import os, sqlite3, sys, time
base = os.path.dirname(os.path.abspath(__file__))
data = os.environ.get("TEST_DATA_DIR", os.path.join(base, "data"))
src = os.path.join(data, "test.db")
if not os.path.exists(src):
    sys.exit("База данных не найдена: " + src)
dst = os.path.join(data, "backup-" + time.strftime("%Y%m%d-%H%M%S") + ".db")
a = sqlite3.connect(src); b = sqlite3.connect(dst)
a.backup(b); b.close(); a.close()
print("Копия сохранена:", dst)
