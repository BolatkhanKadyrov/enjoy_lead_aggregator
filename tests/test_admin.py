import hashlib
import hmac
import io
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from store import Store
from web import WebApp


class AdminTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
