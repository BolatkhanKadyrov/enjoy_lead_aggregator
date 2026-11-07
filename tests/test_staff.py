import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from store import Conflict, MAX_TELEGRAM_ID, PERMISSIONS, Store, employee_id


class StaffStoreTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = Path(temp.name) / "staff.sqlite3"
        self.store = Store(self.database)

    def test_migration_preserves_edits_and_deletions_after_restart(self):
        self.store.seed_staff(None, [1, 2], [3])
        self.assertEqual([], self.store.staff_list())
        self.store.seed_staff(1, [1, 2], [2, 3])
        self.assertEqual(set(PERMISSIONS), self.store.staff_permissions(2))
        self.assertEqual(set(), self.store.staff_permissions(3))
        self.assertIsNone(self.store.staff_permissions(1))
        self.store.save_employee(2, "Повар", ["dishes"],
                                 {"name": "Telegram 2", "permissions": list(PERMISSIONS)})
        self.store.remove_employee(3, {"name": "Telegram 3", "permissions": []})
        reopened = Store(self.database)
        with self.assertRaisesRegex(ValueError, "OWNER_ID"):
            reopened.seed_staff(None, [2, 4], [3])
        reopened.seed_staff(1, [2, 4], [3])
        self.assertEqual([{"id": "2", "name": "Повар", "permissions": ["dishes"]}], reopened.staff_list())

    def test_owner_rotation_does_not_leave_implicit_grants(self):
        self.store.seed_staff(1, [2], [3])
        self.store.seed_staff(2, [1], [3])
        self.assertIsNone(self.store.staff_permissions(2))
        self.assertIsNone(self.store.staff_permissions(1))
        with self.assertRaisesRegex(ValueError, "OWNER_ID"):
            self.store.save_employee("002", "Владелец", [])
        with self.assertRaisesRegex(ValueError, "OWNER_ID"):
            self.store.remove_employee(2, {"name": "Владелец", "permissions": []})
        with self.store.authorized(2, "employees", 2) as available:
            self.assertEqual(set(PERMISSIONS) | {"employees"}, available)
        for uid, permission in [(1, []), (3, "employees"), (3, "hours")]:
            with self.subTest(uid=uid, permission=permission), self.assertRaises(PermissionError):
                with self.store.authorized(uid, permission, 2):
                    self.fail("Unauthorized call")
        with self.store.authorized(3, [], 2):
            self.assertIn("settings", self.store.state())

    def test_ids_and_permissions_fail_closed(self):
        for invalid in [True, 0, -1, "1.0", "1e3", "١٢", " +1", 1.0, MAX_TELEGRAM_ID + 1]:
            with self.subTest(uid=invalid), self.assertRaises(ValueError):
                employee_id(invalid)
        self.assertEqual(MAX_TELEGRAM_ID, employee_id(str(MAX_TELEGRAM_ID)))
        self.assertEqual(123, employee_id("00123"))
        for invalid in [None, "hours", {"hours": True}, [False], [{}], ["employees"], ["hours", "hours"]]:
            with self.subTest(grants=invalid), self.assertRaises(ValueError):
                self.store.save_employee(2, "Повар", invalid)
        for invalid in [True, " ", "a" * 101]:
            with self.subTest(name=invalid), self.assertRaises(ValueError):
                self.store.save_employee(2, invalid, [])
        self.assertEqual([], self.store.staff_list())

    def test_optimistic_staff_updates_and_deletes(self):
        self.store.seed_staff(1, [], [])
        initial = {"name": "Повар", "permissions": ["dishes", "sections"]}
        self.store.save_employee("02", "  Повар ", ["sections", "dishes"])
        self.assertEqual([{"id": "2", **initial}], self.store.staff_list())
        with self.assertRaises(Conflict):
            self.store.save_employee(2, "Дубликат", [])
        self.store.save_employee(2, "Старший повар", list(PERMISSIONS), initial)
        with self.assertRaises(Conflict):
            self.store.remove_employee(2, initial)
        with self.assertRaises(Conflict):
            self.store.save_employee(2, "Повар", [], initial)
        with self.assertRaises(ValueError):
            self.store.remove_employee(2, None)
        self.store.remove_employee(2, {"name": "Старший повар", "permissions": list(PERMISSIONS)})
        with self.assertRaises(Conflict):
            self.store.save_employee(2, "Повар", [], initial)
        self.assertIsNone(self.store.staff_permissions(2))

    def test_point_fields_do_not_overwrite_other_editor(self):
        self.store.save_new_point("Точка", "")
        self.store.edit_point_fields("Точка", {"excluded": True}, {"excluded": False})
        self.store.edit_point_fields("ТОЧКА", {"address": " Дом  7 "}, {"address": ""})
        self.assertEqual({"name": "Точка", "address": "Дом 7", "excluded": True},
                         self.store.catalog()["points"]["точка"])
        with self.assertRaises(Conflict):
            self.store.edit_point_fields("Точка", {"address": "Дом 9"}, {"address": ""})
        for changes, previous in [({"excluded": 1}, {"excluded": False}),
                                  ({"excluded": False}, {"excluded": 1}),
                                  ({"name": "Подмена"}, {"name": "Точка"}),
                                  ({}, {})]:
            with self.subTest(changes=changes, previous=previous), self.assertRaises(ValueError):
                self.store.edit_point_fields("Точка", changes, previous)

    def test_granted_action_and_revocation_use_one_ordered_transaction(self):
        self.store.seed_staff(1, [], [])
        self.store.save_employee(2, "Повар", ["sections"])
        other = Store(self.database)
        other.seed_staff(1, [], [])
        revoking = threading.Event()

        def revoke():
            revoking.set()
            with other.authorized(1, "employees", 1):
                other.remove_employee(2, {"name": "Повар", "permissions": ["sections"]})

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.authorized(2, ["sections"], 1):
                future = pool.submit(revoke)
                self.assertTrue(revoking.wait(timeout=2))
                self.store.add_section("Десерты цеха")
                self.assertIn("Десерты цеха", self.store.state()["sections"])
                self.assertFalse(future.done())
            future.result(timeout=5)
        self.assertIn("Десерты цеха", self.store.state()["sections"])
        with self.assertRaises(PermissionError):
            with self.store.authorized(2, ["sections"], 1):
                self.fail("Revoked write")
        with self.assertRaises(ValueError):
            with self.store.authorized(1, ["sections"], 1):
                self.store.add_section("Откат")
                self.store.add_section(None)
        self.assertNotIn("Откат", self.store.state()["sections"])

    def test_private_answers_are_scoped_to_upload_and_survive_restart(self):
        for uid in (1, 2):
            self.store.replace_job(uid, "v1", [], "2026-10-07")
        self.assertTrue(self.store.save_local_answer(1, "v1", "dish", "Боул", "Боулы"))
        self.assertTrue(self.store.save_local_answer(1, "v1", "point", "Кафе", "Дом 1"))
        self.assertTrue(self.store.save_local_answer(2, "v1", "point", "Кафе", ""))
        reopened = Store(self.database)
        self.assertEqual({"dishes": {"боул": "Боулы"},
                          "points": {"кафе": {"name": "Кафе", "address": "Дом 1", "excluded": False}}},
                         reopened.job_overrides(1, "v1"))
        self.assertNotIn("боул", reopened.catalog()["dishes"])
        self.assertNotIn("кафе", reopened.catalog()["points"])
        reopened.replace_job(1, "v1", [], "2026-10-07")
        empty = {"dishes": {}, "points": {}}
        self.assertEqual(empty, reopened.job_overrides(1, "v1"))
        self.assertEqual("", reopened.job_overrides(2, "v1")["points"]["кафе"]["address"])
        reopened.replace_job(1, "v2", [], "2026-10-07")
        self.assertFalse(reopened.save_local_answer(1, "v1", "dish", "Боул", "Боулы"))
        self.assertEqual(empty, reopened.job_overrides(1, "v2"))
