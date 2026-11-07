import hashlib
import hmac
import io
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from main import owner_from_env
from store import PERMISSIONS, Store
from web import WebApp


class AdminWebCase(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "test.sqlite3"
        self.store = Store(self.database)
        self.token = "test-only-token"
        self.app = WebApp(self.store, self.token, {42})

    def signed(self, uid=42, age=0):
        fields = {"user": json.dumps({"id": uid}), "auth_date": str(int(time.time()) - age),
                  "signature": "sample", "query_id": "example"}
        data = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        key = hmac.new(b"WebAppData", self.token.encode(), hashlib.sha256).digest()
        fields["hash"] = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
        return "tma " + urlencode(fields)

    def request(self, path, body=None, auth=None):
        raw = json.dumps(body).encode() if body is not None else b""
        response = []
        env = {"REQUEST_METHOD": "POST" if raw else "GET", "PATH_INFO": path,
               "HTTP_AUTHORIZATION": self.signed() if auth is None else auth,
               "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(raw)),
               "wsgi.input": io.BytesIO(raw)}
        result = self.app(env, lambda status, headers: response.append(status))
        return int(response[0].split()[0]), json.loads(b"".join(result))


class AdminTests(AdminWebCase):
    def test_only_verified_admin_can_read_or_write(self):
        auth = self.signed()
        for credential in ("", auth + "&user=42", auth.replace("sample", "other"),
                           self.signed(age=3700), self.signed(age=-600)):
            self.assertEqual(401, self.request("/api/state", auth=credential)[0])
        self.assertEqual(403, self.request("/api/state", auth=self.signed(43))[0])
        self.assertEqual(403, self.request("/api/section", {"name": "Недоступно"}, self.signed(43))[0])
        self.assertEqual(200, self.request("/api/state", auth=auth)[0])

    def test_edit_and_conflict_round_trip(self):
        self.store.save_new_point("Новая кофейня", "")
        self.store.save_new_dish("Новое блюдо", "Раздел А")
        self.assertEqual(200, self.request("/api/section", {"name": "Раздел Б"})[0])
        self.assertEqual(200, self.request("/api/section", {"name": "раздел б"})[0])
        p = {"name": "Новая кофейня", "address": "Дом 1", "excluded": True,
             "previous": {"address": "", "excluded": False}}
        self.assertEqual(200, self.request("/api/point", p)[0])
        self.assertEqual(409, self.request("/api/point", {**p, "address": "Дом 2"})[0])
        self.assertEqual(200, self.request("/api/dish", {"name": "Новое блюдо", "section": "РАЗДЕЛ Б",
                         "previous": {"section": "Раздел А"}})[0])
        self.assertEqual(400, self.request("/api/settings", {"start": "23:00", "end": "17:00"})[0])
        self.assertEqual(200, self.request("/api/settings", {"start": "17:30", "end": "22:00",
                         "previous": {"start": "18:00", "end": "23:30"}})[0])
        reopened = Store(self.database)
        self.assertEqual({"start": "17:30", "end": "22:00"}, reopened.settings())
        self.assertEqual("Раздел Б", reopened.catalog()["dishes"]["новое блюдо"])
        point = reopened.catalog()["points"]["новая кофейня"]
        self.assertEqual(("Дом 1", True), (point["address"], point["excluded"]))

    def test_reference_answers_are_insert_only_and_import_is_once(self):
        self.store.save_new_dish("Хлеб", "Мучное")
        self.store.save_new_dish("ХЛЕБ", "Ошибочный раздел")
        self.assertNotIn("Ошибочный раздел", self.store.catalog()["sections"])
        points = [{"name": "Столовая", "address": "Дом 5", "excluded": False}]
        seed = {"sections": ["Горячее"], "dishes": {"Суп дня": "Горячее"}, "points": points}
        with self.assertRaisesRegex(ValueError, "первого запуска"):
            Store(self.database, require_catalog=True)
        first = Store(self.database, initial_catalog=seed, require_catalog=True)
        self.assertEqual("Горячее", first.catalog()["dishes"]["суп дня"])
        first.edit_point("Столовая", "Дом 6", True)
        first.save_new_point("СТОЛОВАЯ", "Дом 1")
        again = Store(self.database, initial_catalog=seed)
        self.assertEqual("Дом 6", again.catalog()["points"]["столовая"]["address"])
        self.assertEqual(first.catalog(), Store(self.database, require_catalog=True).catalog())


class EmployeeAccessTests(AdminWebCase):
    def setUp(self):
        super().setUp()
        self.owner = 42
        self.store.seed_staff(self.owner, {91}, {92})
        self.app = WebApp(self.store, self.token, {91}, owner_id=self.owner)
        self.store.save_new_point("Кофейня", "Дом 1")
        self.store.save_new_dish("Суп", "Горячее")

    def test_each_grant_controls_its_fields_and_action(self):
        scenarios = {
            "point_addresses": ({"points"}, "/api/point", {"name": "Кофейня", "address": "Дом 2"}),
            "invoice_exclusions": ({"points"}, "/api/point", {"name": "Кофейня", "excluded": True}),
            "dishes": ({"dishes", "sections"}, "/api/dish", {"name": "Суп", "section": "Горячее"}),
            "sections": ({"sections"}, "/api/section", {"name": "Выпечка"}),
            "hours": ({"settings"}, "/api/settings", {"start": "17:00", "end": "21:00"}),
        }
        self.assertEqual(set(PERMISSIONS), scenarios.keys())
        for uid, (grant, (readable, path, changes)) in enumerate(scenarios.items(), start=100):
            with self.subTest(grant=grant):
                profile = {"id": str(uid), "name": "Оператор", "permissions": [grant], "previous": None}
                self.assertEqual(200, self.request("/api/employee", profile)[0])
                auth = self.signed(uid)
                status, visible = self.request("/api/state", auth=auth)
                self.assertEqual(200, status)
                self.assertEqual(readable | {"owner", "permissions"}, visible.keys())
                self.assertFalse(visible["owner"])
                self.assertEqual([grant], visible["permissions"])
                if grant in ("point_addresses", "invoice_exclusions"):
                    field = "address" if grant == "point_addresses" else "excluded"
                    self.assertEqual({"name", field}, visible["points"][0].keys())
                    both = {"name": "Кофейня", "address": "Дом 3", "excluded": True}
                    self.assertEqual(403, self.request(path, both, auth)[0])
                for other, (_, other_path, other_changes) in scenarios.items():
                    if other != grant:
                        self.assertEqual(403, self.request(other_path, other_changes, auth)[0])
                self.assertEqual(200, self.request(path, changes, auth)[0])

    def test_employee_management_and_live_revocation(self):
        status, state = self.request("/api/state")
        self.assertEqual(200, status)
        self.assertTrue(state["owner"])
        self.assertEqual({"91", "92"}, {p["id"] for p in state["employees"]})
        profile = {"name": "Алина", "permissions": ["sections"]}
        target = {"id": "93"}
        create = {**target, **profile}
        auth = self.signed(93)
        legacy_admin = self.signed(91)
        self.assertEqual(403, self.request("/api/state", auth=self.signed(92))[0])
        self.assertEqual(403, self.request("/api/state", auth=auth)[0])
        self.assertNotIn("employees", self.request("/api/state", auth=legacy_admin)[1])
        for credential in (auth, legacy_admin):
            self.assertEqual(403, self.request("/api/employee", create, credential)[0])
            self.assertEqual(403, self.request("/api/employee/remove", {**target, "previous": profile}, credential)[0])
        self.assertEqual(200, self.request("/api/employee", create)[0])
        self.assertEqual(409, self.request("/api/employee", create)[0])
        self.assertEqual(200, self.request("/api/section", {"name": "Салаты"}, auth)[0])
        no_edits = {"name": "Алина", "permissions": []}
        self.assertEqual(200, self.request("/api/employee", {**target, **no_edits, "previous": profile})[0])
        self.assertEqual(403, self.request("/api/section", {"name": "Хлеб"}, auth)[0])
        self.assertEqual(403, self.request("/api/state", auth=auth)[0])
        self.assertEqual(409, self.request("/api/employee/remove", {**target, "previous": profile})[0])
        self.assertEqual(200, self.request("/api/employee/remove", {**target, "previous": no_edits})[0])
        self.assertNotIn("93", {p["id"] for p in self.request("/api/state")[1]["employees"]})

    def test_owner_is_only_configurable_in_environment(self):
        body = {"id": "56", "name": "Аккаунт", "permissions": ["employees"]}
        self.assertEqual(400, self.request("/api/employee", body)[0])
        owner = {**body, "permissions": [], "id": "00042"}
        self.assertEqual(400, self.request("/api/employee", owner)[0])
        self.assertEqual(400, self.request("/api/employee/remove", {**owner, "previous": {}})[0])
        self.assertEqual(42, owner_from_env(" 42 "))
        self.assertIsNone(owner_from_env(""))
        for value in ("-42", "42,54", "@name", "0", str(2**52)):
            with self.assertRaisesRegex(ValueError, "OWNER_ID"):
                owner_from_env(value)
        self.app = WebApp(self.store, self.token, {91})
        self.assertEqual(403, self.request("/api/employee", body, self.signed(91))[0])


if __name__ == "__main__":
    unittest.main()
