import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from bot import Bot, MAX_XLSX_BYTES, Telegram, TelegramError
from store import Store
from test_bot import FRIDAY_START, Transport, receipt


class BotUsageTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.clock = patch("store.time.time", return_value=FRIDAY_START - 7200).start()
        self.addCleanup(patch.stopall)
        self.database = Path(folder.name) / "orders.sqlite"
        self.store = Store(self.database)
        self.store.save_new_dish("Суп", "Горячее")
        self.store.save_new_point("Кофейня", "Адрес")
        self.bot = Bot(self.store, "test", set(), "", owner_id=5)
        self.tg = Transport()
        self.bot.api = self.tg
        patch("bot.parse_workbook", side_effect=json.loads).start()
        patch("bot.render_documents", return_value=(b"plan", b"invoices")).start()

    def message(self, seconds, uid=10, **fields):
        self.clock.return_value = FRIDAY_START + seconds
        return {"message_id": self.tg.next_id(), "date": self.clock.return_value,
                "chat": {"id": uid, "type": "private"}, "from": {"id": uid}, **fields}

    def send(self, message):
        self.bot.handle({"message": message})

    def history(self, uid=10):
        rows = self.store.usage_detail(1, uid)["requests"]
        return {r["message_id"]: r for r in rows}

    def test_retries_record_one_upload_with_actual_size_and_workbook_counts(self):
        rows = [receipt(11), receipt(12)]
        rows[0]["lines"].append(receipt(11)["lines"][0])
        raw = json.dumps(rows).encode()
        message = self.message(0, document={"file_id": "x", "file_name": "day.xlsx", "file_size": 7})
        self.tg.delivery_error = TelegramError(429)
        with patch.object(self.tg, "download", side_effect=[TelegramError(503), raw]) as download:
            with self.assertRaises(TelegramError):
                self.send(message)
            pending = self.history()[message["message_id"]]
            self.assertEqual((pending["status"], pending["size_bytes"]), ("pending", 7))
            with self.assertRaises(TelegramError):
                self.send(message)
            # Replacement and metrics are already committed before sending either PDF.
            self.bot.store = self.store = Store(self.database)
            self.send(message)
            self.assertEqual(download.call_count, 2)
        self.assertEqual(len(self.history()), 1)
        saved = self.history()[message["message_id"]]
        self.assertEqual((saved["kind"], saved["status"], saved["size_bytes"],
                          saved["receipts"], saved["lines"]), ("file", "accepted", len(raw), 2, 3))

    def test_all_rejected_attempts_keep_sizes_including_silent_album_members(self):
        invalid = self.message(0, document={"file_id": "bad", "file_name": "bad.xlsx", "file_size": 200})
        self.tg.files["bad"] = b"broken"
        quota = self.message(1, document={"file_id": "unused", "file_name": "day.xlsx", "file_size": 600})
        oversized = self.message(2, document={"file_name": "huge.xlsx", "file_size": 2 * MAX_XLSX_BYTES})
        wrong_type = self.message(3, document={"file_name": "day.txt"})
        album = [self.message(4, document={"file_name": "day.xlsx", "file_size": n}, media_group_id="a")
                 for n in (42, 43)]
        late = self.message(14400, document={"file_name": "day.xlsx", "file_size": 88})
        for message in [invalid, invalid, quota, oversized, wrong_type, *album, *album, late]:
            self.clock.return_value = message["date"]
            self.send(message)
        self.assertEqual(self.tg.downloads, ["bad"])
        rows = self.store.usage_detail(7, 10)["requests"]
        by_message = {r["message_id"]: r["size_bytes"] for r in rows}
        self.assertTrue(all(r["status"] == "rejected" for r in rows))
        self.assertEqual(by_message, {invalid["message_id"]: 6, quota["message_id"]: 600,
                                     oversized["message_id"]: 2 * MAX_XLSX_BYTES, wrong_type["message_id"]: None,
                                     album[0]["message_id"]: 42, album[1]["message_id"]: 43, late["message_id"]: 88})

    def test_resume_is_separate_and_only_eligible_messages_count(self):
        self.bot.operator_ids = {10}
        self.store.save_employee(10, "Сотрудник", [])
        self.tg.files["ok"] = json.dumps([receipt(1)]).encode()
        document = {"file_name": "day.xlsx", "file_id": "ok"}
        self.send(self.message(0, text="/resume"))
        self.send(self.message(1, document=document))
        self.send(self.message(2, text="/resume"))
        accepted = self.message(31, text="/resume")
        self.send(accepted)
        self.send(accepted)
        self.send(self.message(32, text="/id"))
        self.send(self.message(33, text="Ответ на вопрос"))
        self.send(self.message(34, uid=20, document=document))
        self.send(self.message(35, uid=20, text="/resume"))
        self.send(self.message(36, document=document, chat={"id": -10, "type": "group"}))
        rows = self.history().values()
        self.assertEqual(sorted((r["kind"], r["status"]) for r in rows),
                         [("file", "accepted"), ("resume", "accepted"), ("resume", "rejected"),
                          ("resume", "rejected")])
        self.assertTrue(all(r["size_bytes"] is None for r in rows if r["kind"] == "resume"))
        self.assertFalse(self.history(20))

    def test_replayed_preexisting_job_is_not_backfilled(self):
        self.store.replace_job(10, "27", [receipt(1)], "2026-10-09")
        self.store.update_job(10, "27", "done")
        document = {"file_name": "day.xlsx", "file_id": "old"}
        self.send(self.message(0, document=document, message_id=27))
        self.send(self.message(1, document=document, message_id=20))
        self.assertFalse(self.history())

    def test_size_from_get_file_even_when_message_has_no_size(self):
        telegram = Telegram("test")
        size = MAX_XLSX_BYTES * 5
        with patch.object(telegram, "call", return_value={"file_size": size}), \
                patch.object(self.tg, "download", telegram.download):
            self.send(self.message(0, document={"file_name": "day.xlsx", "file_id": "large"}))
        entry, = self.history().values()
        self.assertEqual((entry["status"], entry["size_bytes"]), ("rejected", size))

    def test_confirmed_success_after_early_refusal_supersedes_it_once(self):
        raw = json.dumps([receipt(15)]).encode()
        self.tg.files["ok"] = raw
        message = self.message(-3 * 3600, document={"file_name": "day.xlsx", "file_id": "ok", "file_size": 11})
        with patch.object(self.tg, "call", side_effect=TelegramError(429)):
            with self.assertRaises(TelegramError):
                self.send(message)
        self.assertEqual(self.history()[message["message_id"]]["status"], "rejected")
        self.store.set_settings("15:00", "23:30")
        self.send(message)
        self.send(message)
        # A subsequent refusal must never replace a successfully saved import.
        self.bot.record_request(10, message, "rejected")
        entry, = self.history().values()
        self.assertEqual((entry["status"], entry["size_bytes"], entry["receipts"], entry["lines"]),
                         ("accepted", len(raw), 1, 1))

    def test_poll_ack_closes_lost_or_revoked_pending_without_creating_unlisted_history(self):
        self.bot.operator_ids = {5}
        for uid, replay_original in ((10, True), (11, False)):
            with self.subTest(uid=uid):
                self.store.save_employee(uid, "Сотрудник", [])
                document = {"file_name": "day.xlsx", "file_id": "absent", "file_size": 123}
                original = self.message(0, uid=uid, document=document)
                with patch.object(self.tg, "download", side_effect=TelegramError(503)):
                    with self.assertRaises(TelegramError):
                        self.send(original)
                self.store.remove_employee(uid, {"name": "Сотрудник", "permissions": []})
                self.bot.store = self.store = Store(self.database)
                # The first case gets a retry after access was revoked. In the second,
                # Telegram has discarded the old update and only a new document arrives.
                incoming = original if replay_original else self.message(60, uid=uid, document=document)
                update = {"message": incoming, "update_id": uid * 10}
                stop = Event()
                offsets = []
                statuses = []
                transport = self.tg.call

                def poll(method, fields, files=None):
                    if method in ("deleteWebhook", "setMyCommands"):
                        return True
                    if method != "getUpdates":
                        return transport(method, fields, files)
                    offsets.append(fields["offset"])
                    if len(offsets) <= 2:
                        statuses.append(self.history(uid)[original["message_id"]]["status"])
                        return [] if len(offsets) == 1 else [update]
                    stop.set()
                    return []

                with patch.object(self.tg, "call", side_effect=poll):
                    self.bot.run(stop)
                self.assertEqual(statuses, ["pending", "pending"])
                self.assertEqual(offsets[-1], update["update_id"] + 1)
                row, = self.history(uid).values()
                self.assertEqual((row["message_id"], row["status"]), (original["message_id"], "rejected"))

    def test_partial_oversize_download_discards_incorrect_telegram_size(self):
        telegram = Telegram("test")
        with patch("bot.MAX_XLSX_BYTES", 128), \
                patch.object(telegram, "call", return_value={"file_size": 50, "file_path": "day.xlsx"}), \
                patch("bot.urlopen", return_value=io.BytesIO(b"x" * 200)), \
                patch.object(self.tg, "download", telegram.download):
            self.send(self.message(0, document={"file_name": "day.xlsx", "file_id": "huge", "file_size": 50}))
        row, = self.history().values()
        self.assertEqual((row["status"], row["size_bytes"]), ("rejected", None))


if __name__ == "__main__":
    unittest.main()
