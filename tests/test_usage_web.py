import time
from datetime import datetime
from unittest.mock import patch

from bot import Bot, ZONE
from test_admin import AdminWebCase
from test_bot import Transport
from test_documents import export, row
from web import WebApp


class UsageApiTests(AdminWebCase):
    def setUp(self):
        super().setUp()
        self.store.seed_staff(42, {91}, {92})
        self.app = WebApp(self.store, self.token, {91}, owner_id=42)
        now = int(time.time())
        self.store.record_usage(91, 9, now, "file", "accepted", size_bytes=1500, receipts=2, lines=3)

    def test_owner_can_read_statistics_and_request_sizes(self):
        status, summary = self.request("/api/usage?days=1")
        self.assertEqual(200, status)
        sender = next(u for u in summary["users"] if u["id"] == "91")
        self.assertEqual((1, 1, 1500, 1500), (sender["files"], sender["accepted"], sender["total_bytes"], sender["max_bytes"]))
        status, detail = self.request("/api/usage?days=1&user_id=91")
        self.assertEqual(200, status)
        self.assertEqual(1500, detail["requests"][0]["size_bytes"])
        self.assertEqual([], self.request("/api/usage?days=1&user_id=91&before=9")[1]["requests"])
        self.assertEqual(405, self.request("/api/usage", {})[0])

    def test_statistics_are_visible_only_to_the_configured_owner(self):
        for route in ("/api/usage", "/api/usage?user_id=91"):
            self.assertEqual(401, self.request(route, auth="")[0])
            self.assertEqual(401, self.request(route, auth=self.signed(age=3700))[0])
            for uid in (91, 92, 99):
                self.assertEqual(403, self.request(route, auth=self.signed(uid))[0])
        self.app = WebApp(self.store, self.token, {42, 91})
        self.assertEqual(403, self.request("/api/usage")[0])

    def test_excel_to_pdfs_and_owner_history(self):
        data = export([row(10, "Кофейня", "Суп", 1, 200, 200, 400, 400),
                       row(10, "Кофейня", "Суп", 1, 200, 200, 400, 400),
                       row(11, "Кофейня", "Суп", 3, 200, 600, 600, 600)])
        self.store.save_new_dish("Суп", "Горячее")
        self.store.save_new_point("Кофейня", "Дом 1")
        bot = Bot(self.store, "test", set(), "", owner_id=42)
        bot.api = Transport()
        bot.api.files["example"] = data
        sent = int(datetime.now(ZONE).replace(hour=20, minute=0).timestamp())
        update = {"message": {"chat": {"type": "private", "id": 92}, "from": {"id": 92},
                              "message_id": 7, "date": sent,
                              "document": {"file_name": "example.xlsx", "file_id": "example"}}}
        with patch("store.time.time", return_value=sent):
            bot.handle(update)
            bot.handle(update)
            status, detail = self.request("/api/usage?days=1&user_id=92")
        self.assertEqual(200, status)
        entry, = detail["requests"]
        self.assertEqual((len(data), 2, 3, "accepted"),
                         (entry["size_bytes"], entry["receipts"], entry["lines"], entry["status"]))
        ((_, files),) = bot.api.albums
        self.assertEqual(set(files), {"plan", "invoices"})
        self.assertTrue(all(content.startswith(b"%PDF") for _, content in files.values()))

    def test_invalid_query_is_rejected(self):
        for query in ("days=4", "days=", "days=1&days=30", "days=30&before=1", "user_id=abc",
                      "user_id=42&before=0", "user_id=42&before=100000000000000000000000", "unexpected=1"):
            with self.subTest(query=query):
                self.assertEqual(400, self.request("/api/usage?" + query)[0])
