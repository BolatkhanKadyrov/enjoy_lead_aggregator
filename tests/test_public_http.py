import hashlib
import hmac
import io
import json
import time
import unittest
from unittest.mock import Mock
from urllib.parse import urlencode

from web import WebApp


class PublicHttpTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.token = "test-only"
        self.app = WebApp(self.store, self.token, {42})

    def request(self, path, method="GET", auth=""):
        result = []
        body = self.app({"PATH_INFO": path, "REQUEST_METHOD": method, "HTTP_AUTHORIZATION": auth,
                         "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": "2",
                         "wsgi.input": io.BytesIO(b"{}")},
                        lambda status, headers: result.append((int(status.split()[0]), dict(headers))))
        return *result[0], b"".join(body)

    def test_public_pages_and_private_files(self):
        for path in ("/", "/static/app.js", "/static/style.css", "/health"):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertEqual(headers["Cache-Control"], "no-store")
                if path == "/health":
                    self.assertEqual(json.loads(body), {"ok": True})
        for path in ("/.env", "/data/enjoy.sqlite3", "/enjoy-catalog.json", "/static/enjoy-catalog.json",
                     "/seed.json", "/example.xlsx", "/example.pdf"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)
        self.assertFalse(self.store.mock_calls)

    def test_all_admin_routes_check_identity_before_storage(self):
        fields = {"user": json.dumps({"id": 7}), "auth_date": str(int(time.time()))}
        signed = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
        secret = hmac.digest(b"WebAppData", self.token.encode(), hashlib.sha256)
        fields["hash"] = hmac.new(secret, signed.encode(), hashlib.sha256).hexdigest()
        ordinary_user = "tma " + urlencode(fields)
        for path, method in (("/api/state", "GET"), ("/api/point", "POST"), ("/api/section", "POST"),
                             ("/api/dish", "POST"), ("/api/settings", "POST")):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, method)[0], 401)
                self.assertEqual(self.request(path, method, ordinary_user)[0], 403)
        self.assertFalse(self.store.mock_calls)
