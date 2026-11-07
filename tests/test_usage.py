import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

from store import Store


def local_timestamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Almaty")).timestamp())


class UsageTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.path = Path(folder) / "usage.sqlite"
        self.now = local_timestamp("2026-11-08T00:15")
        self.clock = self.enterContext(patch("store.time.time", return_value=self.now))
        self.store = Store(self.path)

    def by_id(self, days=1):
        return {row["id"]: row for row in self.store.usage_summary(days, 100)["users"]}

    def test_idempotent_accounting_for_all_sizes_and_admission_statuses(self):
        self.store.save_employee(101, "Ирина", [])
        self.store.save_employee(102, "Алина", [])
        self.store.record_usage(101, 1, self.now, "file", "pending", size_bytes=940)
        entries = [{"lines": [{}, {}]}, {"lines": [{}]}]
        self.store.replace_job(101, "1", entries, "2026-11-08", usage_sent_at=self.now, usage_size_bytes=900)
        self.store.record_usage(101, 1, self.now, "file", "pending", size_bytes=940)
        self.store.record_usage(101, 1, self.now, "file", "rejected", size_bytes=500)
        self.store.record_usage(101, 2, self.now, "file", "rejected", size_bytes=25_000_000)
        self.store.record_usage(101, 3, self.now, "file", "pending")
        self.store.record_usage(101, 4, self.now, "resume", "accepted")
        self.store.record_usage(101, 5, self.now, "resume", "rejected")
        self.store.record_usage(101, 6, self.now, "file", "pending", size_bytes=20)
        self.store.record_work_error(101, 6, "Некорректная таблица", usage_sent_at=self.now)
        self.store.record_usage(103, 1, self.now, "file", "rejected")
        self.store = Store(self.path)
        users = self.by_id()
        self.assertEqual(set(users), {"100", "101", "102", "103"})
        self.assertEqual(users["100"]["name"], "Владелец")
        self.assertEqual(users["103"]["name"], "Telegram 103")
        self.assertEqual((users["102"]["files"], users["102"]["max_bytes"]), (0, None))
        self.assertEqual(users["101"], {
            "id": "101", "name": "Ирина", "files": 4, "accepted": 1, "rejected": 2, "pending": 1,
            "resumes": 2, "total_bytes": 25_000_920, "max_bytes": 25_000_000, "unknown_sizes": 1,
            "receipts": 2, "lines": 3,
        })
        detail = self.store.usage_detail(1, 101)["requests"]
        self.assertEqual(detail[0]["size_bytes"], 20)
        self.assertEqual(detail[-1], {"message_id": 1, "sent_at": self.now, "kind": "file", "status": "accepted",
                                     "size_bytes": 900, "receipts": 2, "lines": 3})
        self.assertEqual(self.store.get_job(101)["receipts"], entries)

    def test_astana_periods_start_at_local_midnight_and_prune(self):
        timestamps = ["2026-10-09T23:59:59", "2026-10-10T00:00", "2026-11-01T23:59:59",
                      "2026-11-02T00:00", "2026-11-07T23:59:59", "2026-11-08T00:00"]
        for msgid, timestamp in enumerate(timestamps, start=1):
            self.store.record_usage(101, msgid, local_timestamp(timestamp), "file", "rejected", size_bytes=10)
        self.assertEqual([self.by_id(days)["101"]["files"] for days in (1, 7, 30)], [1, 3, 5])
        self.assertEqual(self.store.usage_summary(7, 100)["since"], "2026-11-02")
        self.clock.return_value = local_timestamp("2026-11-09T00:01")
        self.store.record_usage(101, 7, self.clock.return_value, "file", "rejected")
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT MIN(message_id) FROM usage_events").fetchone()[0], 3)
        self.assertEqual(self.store.usage_summary(1, 100)["started_at"], self.now)
        self.clock.return_value = local_timestamp("2026-12-20T12:00")
        self.store = Store(self.path)
        self.assertEqual(self.store.usage_summary(1, 100)["started_at"], self.now)
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0], 0)
        for days in (0, 2, 31, True):
            with self.subTest(days=days), self.assertRaises(ValueError):
                self.store.usage_summary(days, 100)

    def test_cursor_pages_stay_disjoint_after_new_upload(self):
        for i in range(1, 52):
            self.store.record_usage(101, i, self.now, "file", "accepted", size_bytes=i)
        self.store.record_usage(102, 99, self.now, "file", "accepted")
        first = self.store.usage_detail(1, 101)
        self.store.record_usage(101, 52, self.now, "file", "accepted")
        second = self.store.usage_detail(1, 101, first["next_before"])
        last = self.store.usage_detail(1, 101, second["next_before"])
        self.assertEqual([page["next_before"] for page in (first, second, last)], [27, 2, None])
        ids = [r["message_id"] for page in (first, second, last) for r in page["requests"]]
        self.assertEqual(ids, list(range(51, 0, -1)))
        self.assertEqual(self.store.usage_detail(1, 100), {"requests": [], "next_before": None})
        for before in (0, -1, True, 2**63, "5"):
            with self.subTest(before=before), self.assertRaises(ValueError):
                self.store.usage_detail(1, 101, before)

    def test_concurrent_telegram_replays_and_existing_database(self):
        self.store.save_employee(101, "Оператор", [])
        self.store.replace_job(101, "1", [], "2026-11-08")  # an upload from before statistics existed
        with self.store.connection(write=True) as conn:
            conn.execute("DROP TABLE usage_events")
            conn.execute("DELETE FROM config WHERE key='usage_started_at'")
        self.store = Store(self.path)
        self.assertEqual(self.by_id()["101"]["files"], 0)
        self.assertEqual(self.store.job_version(101), "1")

        def retry(_):
            self.store.record_usage(101, 2, self.now, "file", "pending", size_bytes=12)
            self.store.record_usage(101, 2, self.now, "file", "accepted", size_bytes=10, receipts=2, lines=3)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(retry, range(12)))
        row = self.by_id()["101"]
        self.assertEqual((row["files"], row["accepted"], row["total_bytes"], row["lines"]), (1, 1, 10, 3))


if __name__ == "__main__":
    unittest.main()
