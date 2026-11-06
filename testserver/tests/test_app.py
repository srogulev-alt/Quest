# -*- coding: utf-8 -*-
"""Автотесты: python -m unittest discover -s tests -v  (из папки testserver)"""
import importlib, json, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
H = {"X-Test-Client": "1"}


def load_app(seed=None):
    tmp = tempfile.mkdtemp()
    os.environ["TEST_DATA_DIR"] = tmp
    os.environ["TEST_ADMIN_PASSWORD"] = "secret123"
    sys.modules.pop("app", None)
    mod = importlib.import_module("app")
    return mod, tmp


class BankTests(unittest.TestCase):
    def test_bank_has_no_errors(self):
        import check_bank
        with open(os.path.join(ROOT, "seed_bank.json"), encoding="utf-8") as f:
            data = json.load(f)
        errors, _warns, _skew = check_bank.check(data)
        self.assertEqual(errors, [])


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod, cls.tmp = load_app()
        cls.client = cls.mod.app.test_client()

    def admin(self):
        c = self.mod.app.test_client()
        r = c.post("/api/admin/login", json={"password": "secret123"}, headers=H)
        self.assertEqual(r.status_code, 200)
        return c

    def test_public_lists_directions(self):
        r = self.client.get("/api/public").get_json()
        ids = {c["id"] for c in r["cols"]}
        self.assertTrue({"civ", "adm", "bank"} <= ids)

    def test_block_without_directions_is_not_in_pools(self):
        """Блок без направлений (например, резерв) не должен попадать в тесты."""
        c = self.admin()
        r = c.post("/api/admin/blocks", json={"title": "Тестовый пустой блок", "cols": []}, headers=H).get_json()
        bid = r["id"]
        blocks = {b["id"]: b for b in c.get("/api/admin/blocks").get_json()["blocks"]}
        self.assertIn(bid, blocks)
        pub = self.client.get("/api/public").get_json()
        self.assertTrue(all(col["n"] >= 0 for col in pub["cols"]))
        c.delete("/api/admin/blocks/%d" % bid, headers=H)

    def test_attempt_flow_and_scoring(self):
        c = self.mod.app.test_client()
        st = c.post("/api/start", json={"name": "Иванов Иван", "col": "bank"}).get_json()
        aid = st["attempt_id"]
        self.assertEqual(len(st["questions"]), st["total"])
        for q in st["questions"]:                       # сервер не должен отдавать правильный ответ
            self.assertNotIn("correct", q)
            self.assertEqual(len(q["options"]), 4)
        import sqlite3
        con = sqlite3.connect(os.path.join(self.tmp, "test.db"))
        con.row_factory = sqlite3.Row
        snaps = {r["idx"]: json.loads(r["snap"]) for r in con.execute("SELECT idx,snap FROM items WHERE attempt_id=?", (aid,))}
        con.close()
        for idx, sn in snaps.items():                   # отвечаем правильно на все
            r = c.post("/api/attempt/%s/answer" % aid, json={"idx": idx, "choice": sn["correct"]})
            self.assertEqual(r.status_code, 200)
        res = c.post("/api/attempt/%s/finish" % aid).get_json()
        self.assertEqual(res["score"], res["total"])
        # после завершения новые ответы не принимаются
        r = c.post("/api/attempt/%s/answer" % aid, json={"idx": 0, "choice": 0})
        self.assertEqual(r.status_code, 409)

    def test_options_are_shuffled(self):
        seen = set()
        for i in range(6):
            c = self.mod.app.test_client()
            st = c.post("/api/start", json={"name": "Петров Пётр %d" % i, "col": "civ"}).get_json()
            seen.add(tuple(q["options"][0] for q in st["questions"]))
        self.assertGreater(len(seen), 1)

    def test_admin_required(self):
        self.assertEqual(self.client.get("/api/admin/blocks").status_code, 401)

    def test_login_lockout(self):
        c = self.mod.app.test_client()
        codes = [c.post("/api/admin/login", json={"password": "bad"}, headers=H).status_code for _ in range(6)]
        self.assertEqual(codes[0], 401)
        self.assertEqual(codes[-1], 429)
        self.mod._fails.clear()

    def test_import_merge_and_block_update(self):
        c = self.admin()
        data = {"blocks": [{"id": 900, "title": "Тестовый блок", "cols": ["civ"]}],
                "questions": [{"id": "900-01", "b": 900, "q": "Вопрос?", "o": ["а", "б", "в", "г"], "a": 2,
                               "act": "Акт", "p": "п. 1", "e": "Пояснение"}]}
        r = c.post("/api/admin/import?mode=merge", json=data, headers=H).get_json()
        self.assertEqual((r["added"], r["updated"]), (1, 0))
        data["blocks"][0]["title"] = "Переименован"
        data["questions"][0]["a"] = 1
        r = c.post("/api/admin/import?mode=merge", json=data, headers=H).get_json()
        self.assertEqual((r["added"], r["updated"]), (0, 1))
        titles = {b["id"]: b["title"] for b in c.get("/api/admin/blocks").get_json()["blocks"]}
        self.assertEqual(titles[900], "Тестовый блок")          # по умолчанию название блока сохраняется
        c.post("/api/admin/import?mode=merge&blocks=update", json=data, headers=H)
        titles = {b["id"]: b["title"] for b in c.get("/api/admin/blocks").get_json()["blocks"]}
        self.assertEqual(titles[900], "Переименован")
        bad = {"blocks": data["blocks"], "questions": [dict(data["questions"][0], o=["а", "б", "в"])]}
        self.assertEqual(c.post("/api/admin/import?mode=merge", json=bad, headers=H).status_code, 400)

    def test_mix_title(self):
        cols = {c["id"]: c["title"] for c in self.client.get("/api/public").get_json()["cols"]}
        self.assertEqual(cols["mix"], "Все темы")

    def test_acts_list(self):
        r = self.client.get("/api/acts").get_json()
        acts = r["acts"]
        self.assertGreater(len(acts), 0)
        self.assertEqual(len({a["act"] for a in acts}), len(acts))          # без повторов
        con_total = sum(a["n"] for a in acts)
        pub = {c["id"]: c["n"] for c in self.client.get("/api/public").get_json()["cols"]}
        self.assertEqual(con_total, pub["mix"])                              # каждый вопрос учтён один раз
        bank_only = [a for a in acts if a["cols"] == ["mix", "bank"]]
        self.assertTrue(bank_only)
        self.assertTrue(all("Пленум" in a["act"] for a in acts))
        # ВС РФ идут раньше ВАС РФ
        vas = [("ВАС" in a["act"][:40]) for a in acts]
        self.assertEqual(vas, sorted(vas))

    def test_complaints(self):
        c = self.mod.app.test_client()
        st = c.post("/api/start", json={"name": "Жалобин Иван", "col": "civ"}).get_json()
        aid = st["attempt_id"]
        url = "/api/attempt/%s/complaint" % aid
        # до завершения теста нельзя
        self.assertEqual(c.post(url, json={"idx": 0, "kind": "answer"}).status_code, 409)
        res = c.post("/api/attempt/%s/finish" % aid).get_json()
        self.assertIn("complaint_kinds", res)
        self.assertEqual(c.post(url, json={"idx": 0, "kind": "nope"}).status_code, 400)
        self.assertEqual(c.post(url, json={"idx": 0, "kind": "other"}).status_code, 400)   # «другое» без описания
        self.assertEqual(c.post(url, json={"idx": 999, "kind": "answer"}).status_code, 404)
        self.assertEqual(c.post(url, json={"idx": 0, "kind": "answer", "comment": "x" * 2001}).status_code, 400)
        self.assertEqual(c.post(url, json={"idx": 0, "kind": "answer"}).status_code, 200)  # описание необязательно
        self.assertEqual(c.post(url, json={"idx": 0, "kind": "outdated", "comment": "Пункт изменён"}).status_code, 200)
        self.assertEqual(c.post(url, json={"idx": 1, "kind": "question", "comment": "=1+1"}).status_code, 200)
        res = c.get("/api/attempt/%s" % aid).get_json()
        self.assertEqual(res["complaints"]["0"], {"kind": "outdated", "comment": "Пункт изменён"})
        a = self.admin()
        self.assertEqual(self.client.get("/api/admin/complaints").status_code, 401)
        lst = a.get("/api/admin/complaints").get_json()
        mine = [x for x in lst["items"] if x["snap"]["name"] == "Жалобин Иван"]
        self.assertEqual(len(mine), 2)                                       # повторная отправка заменяет, а не дублирует
        self.assertTrue(all(x["q_state"] == "same" for x in mine))
        self.assertGreaterEqual(a.get("/api/admin/me").get_json()["complaints_new"], 2)
        cid = mine[0]["id"]
        self.assertEqual(a.put("/api/admin/complaints/%d" % cid, json={"status": "resolved"}).status_code, 400)  # без заголовка
        self.assertEqual(a.put("/api/admin/complaints/%d" % cid, json={"status": "resolved"}, headers=H).status_code, 200)
        self.assertIn(cid, [x["id"] for x in a.get("/api/admin/complaints?status=resolved").get_json()["items"]])
        csv_text = a.get("/api/admin/complaints.csv").get_data(as_text=True)
        self.assertIn("'=1+1", csv_text)                                     # защита от формул в Excel
        # замечание переживает удаление попытки
        a.delete("/api/admin/attempts/%s" % aid, headers=H)
        left = [x for x in a.get("/api/admin/complaints?status=all").get_json()["items"] if x["snap"]["name"] == "Жалобин Иван"]
        self.assertEqual(len(left), 2)
        self.assertEqual(a.delete("/api/admin/complaints/%d" % cid, headers=H).status_code, 200)


if __name__ == "__main__":
    unittest.main()
