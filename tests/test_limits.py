import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch

from store import INTAKE_RETENTION, IntakeLimits, Store


class IntakeTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.database = Path(tmp.name) / "orders.sqlite"
        self.store = Store(self.database)
        self.clock = self.enterContext(patch("store.time.time", return_value=1000))

    def test_global_quota_under_concurrent_users(self):
        parallel = 9
        barrier = Barrier(parallel)
        limits = IntakeLimits(global_rate=3)

        def submit(uid):
            barrier.wait(10)
            return self.store.reserve_work(uid, 1, 1000, limits)

        with ThreadPoolExecutor(max_workers=parallel) as pool:
            results = list(pool.map(submit, range(1, parallel + 1)))
        self.assertEqual(results.count((None, False)), 3)
        self.assertEqual(results.count((60, False)), 6)
        self.clock.return_value = 1060
        self.assertEqual(self.store.reserve_work(11, 1, 1060, limits), (None, False))

    def test_repeated_message_and_resume_cost_one_slot_after_restart(self):
        limits = IntakeLimits(cooldown=60)
        self.store.replace_job(7, "5", [], "2026-10-07")
        self.store.update_job(7, "5", "question", {"name": "Новое блюдо"})
        self.assertEqual(self.store.job_version(7), "5")
        self.assertIsNone(self.store.job_version(8))
        self.assertEqual(self.store.reserve_work(7, 6, 1000, limits, resume=True), (None, False))
        job = self.store.get_job(7)
        self.assertEqual((job["status"], job["pending"]), ("processing", None))
        self.store.update_job(7, "5", "done")
        reopened = Store(self.database)
        self.assertEqual(reopened.reserve_work(7, 6, 1000, limits, resume=True), (None, True))
        self.assertEqual(reopened.reserve_work(7, 7, 1000, limits, resume=True), (60, False))
        self.assertEqual(reopened.get_job(7)["status"], "done")

    def test_both_clock_windows_and_inclusive_boundary(self):
        limits = IntakeLimits(cooldown=30)
        self.assertEqual(self.store.reserve_work(7, 1, 10, limits), (None, False))
        self.clock.return_value = 1005
        # Distant send timestamps arrive in the same processing burst.
        self.assertEqual(self.store.reserve_work(7, 2, 40, limits), (25, False))
        self.clock.return_value = 1030
        # A burst of messages sent together is processed slowly.
        self.assertEqual(self.store.reserve_work(7, 3, 11, limits), (0, False))
        self.assertEqual(self.store.reserve_work(7, 4, 40, limits), (None, False))

    def test_per_sender_rolling_window_preserves_previous_job_on_refusal(self):
        limits = IntakeLimits(cooldown=1, per_user=2)
        self.store.replace_job(7, "90", [], "2026-10-07")
        self.store.update_job(7, "90", "question", {"name": "Новое блюдо"})
        for timestamp in (1000, 1001):
            self.clock.return_value = timestamp
            self.assertEqual(self.store.reserve_work(7, timestamp, timestamp, limits), (None, False))
        self.clock.return_value = 1599
        self.assertEqual(self.store.reserve_work(7, 3, 1599, limits, resume=True), (1, False))
        job = self.store.get_job(7)
        self.assertEqual((job["status"], job["pending"]), ("question", {"name": "Новое блюдо"}))
        self.clock.return_value = 1600
        self.assertEqual(self.store.reserve_work(7, 4, 1600, limits), (None, False))

    def test_processing_only_denial_survives_replay_and_uses_one_row_per_sender(self):
        limits = IntakeLimits()
        self.assertEqual(self.store.reserve_work(7, 1, 10, limits), (None, False))
        self.clock.return_value = 1005
        for message_id in range(2, 12):
            self.assertEqual(self.store.reserve_work(7, message_id, 40, limits), (25, False))
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM intake_denials").fetchone()[0], 1)
        reopened = Store(self.database)
        self.clock.return_value = 1010
        self.assertEqual(reopened.reserve_work(7, 11, 40, limits), (20, True))
        self.clock.return_value = 1030
        self.assertEqual(reopened.reserve_work(7, 11, 40, limits), (0, True))
        self.assertEqual(reopened.reserve_work(7, 12, 1030, limits), (None, False))
        self.clock.return_value += INTAKE_RETENTION
        reopened.reserve_work(9, 1, 9000, limits)
        with reopened.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM intake_denials").fetchone()[0], 0)

    def test_idempotent_errors_and_expiry(self):
        limits = IntakeLimits()
        self.store.reserve_work(1, 20, 1000, limits)
        self.store.record_work_error(1, 20, "Некорректный файл.")
        self.assertEqual(Store(self.database).work_error(1, 20), "Некорректный файл.")
        self.assertIsNone(self.store.work_error(2, 20))
        self.clock.return_value += INTAKE_RETENTION
        self.store.reserve_work(2, 22, 1001, limits)
        self.assertIsNone(self.store.work_error(1, 20))

    def test_notices_coalesced_without_suppressing_later_messages(self):
        self.assertTrue(self.store.should_notify(7, 2000, album="group"))
        self.assertTrue(self.store.should_notify(8, 2000, album="group"))
        self.clock.return_value += 10
        self.assertFalse(self.store.should_notify(7, 2010))
        self.clock.return_value += 25
        reopened = Store(self.database)
        self.assertFalse(reopened.should_notify(7, 2010))
        self.assertFalse(reopened.should_notify(7, 2035, album="group"))
        self.assertTrue(reopened.should_notify(7, 2035))
        self.clock.return_value += INTAKE_RETENTION
        self.assertTrue(reopened.should_notify(7, 2035 + INTAKE_RETENTION, album="group"))

    def test_configuration_bounds(self):
        for data in ({"cooldown": False}, {"per_user": 0}, {"global_rate": 10001}, {"cooldown": 86401}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                IntakeLimits(**data)

    def test_definitively_failed_notice_can_retry_without_releasing_other_notices(self):
        self.assertTrue(self.store.should_notify(7, 2000, album="group"))
        self.assertTrue(self.store.should_notify(8, 2000, album="group"))
        self.store.release_notice(7, 2001, album="group")
        self.store.release_notice(7, 2000)
        self.store.release_notice(7, 2000, album="other")
        self.assertFalse(self.store.should_notify(7, 2000, album="group"))
        self.store.release_notice(7, 2000, album="group")
        reopened = Store(self.database)
        self.assertTrue(reopened.should_notify(7, 2000, album="group"))
        self.assertFalse(reopened.should_notify(8, 2000, album="group"))
        self.clock.return_value += 31
        self.assertTrue(reopened.should_notify(7, 2031))
        reopened.release_notice(7, 2000, album="group")
        self.assertFalse(reopened.should_notify(7, 2031))
        reopened.release_notice(7, 2031)
        self.assertTrue(reopened.should_notify(7, 2031))
