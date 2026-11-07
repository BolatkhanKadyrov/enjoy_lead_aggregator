from datetime import date, datetime
from email.parser import BytesParser
from email.policy import default
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from bot import Bot, TelegramError, ZONE, multipart
from store import Store


def receipt(number, point="Кофейня", dish="Суп"):
    return {"id": str(number), "point": point, "gross": "200", "discount": "0", "paid": "200",
            "lines": [{"name": dish, "exchange": False, "qty": 1, "price": "200", "discount": "0", "total": "200"}]}


class Transport:
    """A private chat with monotonically increasing Telegram message IDs."""

    def __init__(self):
        self.sequence = 0
        self.files = {}
        self.messages = []
        self.albums = []
        self.delivery_error = None

    def next_id(self):
        self.sequence += 1
        return self.sequence

    def download(self, file_id):
        return self.files[file_id]

    def call(self, method, fields, files=None):
        if method == "answerCallbackQuery":
            return True
        if method == "sendMessage":
            msg = dict(fields, message_id=self.next_id())
            self.messages.append(msg)
            return msg
        if method == "sendMediaGroup":
            if self.delivery_error:
                error = self.delivery_error
                self.delivery_error = None
                raise error
            self.albums.append((fields, files))
            return [{"message_id": self.next_id()}, {"message_id": self.next_id()}]
        raise AssertionError(method)


class BotTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "orders.sqlite"
        self.store = Store(self.db_path)
        self.store.save_new_point("Кофейня", "Тестовый адрес")
        self.store.save_new_dish("Суп", "Горячее")
        self.tg = Transport()
        self.bot = Bot(self.store, "test", {500}, "https://mini.example")
        self.bot.api = self.tg
        self.parse = patch("bot.parse_workbook", side_effect=lambda raw: json.loads(raw)).start()
        self.draw = patch("bot.render_documents", return_value=(b"%PDF-plan", b"%PDF-invoices")).start()
        self.addCleanup(patch.stopall)

    def message(self, uid=100, when="2026-10-09 20:00", **kwargs):
        sent = datetime.fromisoformat(when).replace(tzinfo=ZONE)
        return {"message_id": self.tg.next_id(), "date": int(sent.timestamp()), "from": {"id": uid},
                "chat": {"id": uid, "type": "private"}, **kwargs}

    def upload(self, rows, uid=100, when="2026-10-09 20:00"):
        file_id = str(len(self.tg.files))
        self.tg.files[file_id] = json.dumps(rows).encode()
        msg = self.message(uid, when, document={"file_id": file_id, "file_name": "orders.XLSX"})
        self.bot.handle({"message": msg})
        return msg

    def press(self, prompt, row=0, uid=100):
        button = prompt["reply_markup"]["inline_keyboard"][row][0]
        self.bot.handle({"callback_query": {"from": {"id": uid}, "id": "callback",
            "message": {"chat": {"type": "private", "id": uid}, "message_id": prompt["message_id"]},
            "data": button["callback_data"]}})

    def test_last_full_workbook_per_user_and_friday_delivery(self):
        self.upload([receipt(101), receipt(102)])
        self.upload([receipt(501)], uid=200)
        msg = self.upload([receipt(101), receipt(103)])
        self.assertEqual(self.store.get_job(100)["receipts"], [receipt(101), receipt(103)])
        self.assertEqual(self.store.get_job(200)["receipts"], [receipt(501)])
        self.assertEqual(self.draw.call_args.args[2:], (date(2026, 10, 9), date(2026, 10, 12)))
        self.assertEqual([f["chat_id"] for f, _ in self.tg.albums], [100, 200, 100])
        fields, files = self.tg.albums[-1]
        self.assertEqual(json.loads(fields["reply_parameters"])["message_id"], msg["message_id"])
        self.assertEqual([m["media"] for m in json.loads(fields["media"])], ["attach://plan", "attach://invoices"])
        self.assertTrue(all(name.endswith("2026-10-12.pdf") for name, _ in files.values()))
        self.bot.handle({"message": msg})
        self.assertEqual(len(self.tg.albums), 3, "replayed Telegram update must not send a second album")

    def test_submission_hours_weekend_and_invalid_upload_preserve_latest(self):
        self.upload([receipt(1)], when="2026-10-08 18:00:00")
        self.upload([receipt(2)], when="2026-10-08 23:30:59")
        for when in ("2026-10-08 17:59", "2026-10-08 23:31", "2026-10-10 19:00"):
            self.upload([receipt(3)], when=when)
        self.store.set_settings("17:00", "19:00")
        self.upload([receipt(4)], when="2026-10-09 17:15")
        self.parse.side_effect = ValueError("неверный заголовок")
        self.upload([receipt(99)], when="2026-10-09 17:15")
        self.assertIn("неверный заголовок", self.tg.messages[-1]["text"])
        self.assertEqual([r["id"] for r in self.store.get_job(100)["receipts"]], ["4"])
        self.assertEqual(len(self.tg.albums), 3)

    def test_weekend_refusal_uses_telegram_timestamp_in_astana(self):
        for utc, local_time in (("2026-11-07T17:45:00+00:00", "22:45"),
                                ("2026-11-06T19:15:00+00:00", "00:15")):
            with self.subTest(utc=utc):
                message = self.message(date=int(datetime.fromisoformat(utc).timestamp()),
                                       document={"file_id": "unused", "file_name": "day.xlsx"})
                self.bot.handle({"message": message})
                reply = self.tg.messages[-1]["text"].lower()
                for detail in ("суббота", "07.11.2026", local_time, "09.11.2026 в 18:00"):
                    self.assertIn(detail, reply)
        self.parse.assert_not_called()

    def test_weekday_refusal_reports_local_time_and_configured_hours(self):
        self.store.set_settings("18:15", "22:50")
        self.upload([receipt(4)], when="2026-11-09 17:59")
        reply = self.tg.messages[-1]["text"].lower()
        for detail in ("09.11.2026", "17:59", "18:15", "22:50"):
            self.assertIn(detail, reply)
        self.assertNotIn("выходной", reply)
        self.parse.assert_not_called()

    def test_replies_stale_callbacks_and_new_section_survive_restart(self):
        self.upload([receipt(1, "Точка A", "Новое A")])
        old = self.tg.messages[-1]
        self.upload([receipt(2, "Точка Б", "Новое Б")])
        new = self.tg.messages[-1]
        self.press(old)
        self.bot.handle({"message": self.message(text="Старый ответ", reply_to_message={"message_id": old["message_id"]})})
        self.assertNotIn("новое б", self.store.catalog()["dishes"])
        self.bot = Bot(Store(self.db_path), "test", set(), "")
        self.bot.api = self.tg
        self.bot.handle({"message": self.message(text="  Десерты  ", reply_to_message={"message_id": new["message_id"]})})
        self.assertEqual(self.store.catalog()["dishes"]["новое б"], "Десерты")
        self.assertNotIn("новое a", self.store.catalog()["dishes"])
        address_prompt = self.tg.messages[-1]
        self.assertIn("Точка Б", address_prompt["text"])
        self.press(address_prompt)
        self.assertEqual(self.store.catalog()["points"]["точка б"]["address"], "")
        self.assertEqual(len(self.tg.albums), 1)
        self.upload([receipt(3, "Точка Б", "Новое В")], uid=200)
        self.bot.handle({"message": self.message(200, text="ДЕСЕРТЫ")})
        self.assertEqual(self.store.catalog()["dishes"]["новое в"], "Десерты")
        self.assertEqual(len(self.tg.albums), 2)

    def test_button_section_and_typed_address(self):
        self.upload([receipt(1, "Точка В", "Блюдо В")])
        prompt = self.tg.messages[-1]
        section = prompt["reply_markup"]["inline_keyboard"][0][0]["text"]
        self.press(prompt)
        self.bot.handle({"message": self.message(text="Проспект 12")})
        self.assertEqual(self.store.catalog()["dishes"]["блюдо в"], section)
        self.assertEqual(self.store.catalog()["points"]["точка в"]["address"], "Проспект 12")
        self.assertEqual(len(self.tg.albums), 1)

    def test_delivery_retry_and_admin_mini_app_only_in_private_chat(self):
        self.tg.delivery_error = TelegramError(503)
        self.upload([receipt(2)])
        self.assertEqual(self.store.get_job(100)["status"], "retry")
        self.bot.handle({"message": self.message(text="/resume")})
        self.assertEqual(len(self.tg.albums), 1)
        for uid in (100, 500):
            self.bot.handle({"message": self.message(uid=uid, text="/start")})
        self.assertNotIn("reply_markup", self.tg.messages[-2])
        self.assertEqual(self.tg.messages[-1]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"],
                         "https://mini.example")
        count = len(self.tg.messages)
        self.bot.handle({"message": self.message(uid=500, chat={"type": "group", "id": -123}, text="/admin")})
        self.assertEqual(len(self.tg.messages), count)

    def test_multipart_transmits_pdf_bytes_and_utf8_filename(self):
        document = b"%PDF-\x00\r\n\xff"
        data, content_type = multipart({"chat_id": 42}, {"plan": ("План.pdf", document)})
        envelope = BytesParser(policy=default).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode() + data)
        parts = list(envelope.iter_parts())
        self.assertEqual(parts[0].get_payload(decode=True), b"42")
        self.assertEqual(parts[1].get_filename(), "План.pdf")
        self.assertEqual(parts[1].get_payload(decode=True), document)

    def test_polling_retries_rate_limit_with_same_cursor_then_confirms(self):
        self.tg.files["latest"] = json.dumps([receipt(123)]).encode()
        update = {"update_id": 17, "message": self.message(document={"file_id": "latest", "file_name": "day.xlsx"})}
        stop = Event()
        offsets = []
        transport_call = self.tg.call

        def respond(method, fields, files=None):
            if method == "deleteWebhook":
                self.assertIs(fields["drop_pending_updates"], False)
                return True
            if method == "getUpdates":
                offsets.append(fields["offset"])
                if fields["offset"] == 18:
                    stop.set()
                    return []
                return [update]
            return transport_call(method, fields, files)

        self.tg.call = respond
        self.tg.delivery_error = TelegramError(429, 2)
        with patch.object(stop, "wait", return_value=False) as wait, self.assertLogs("bot", level="WARNING"):
            self.bot.run(stop)
        wait.assert_called_once_with(2)
        self.assertEqual(offsets, [0, 0, 18])
        self.assertEqual(len(self.tg.albums), 1)
        self.assertEqual(Store(self.db_path).get_offset(), 18)


if __name__ == "__main__":
    unittest.main()
