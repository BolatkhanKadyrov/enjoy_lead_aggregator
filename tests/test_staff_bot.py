from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bot import Bot
from store import Store
from test_bot import FRIDAY_START, Transport, receipt


class StaffBotTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "orders.sqlite"
        self.store = Store(self.path)
        self.api = Transport()
        self.api.files["orders"] = b"xlsx"
        self.clock = patch("store.time.time", return_value=FRIDAY_START).start()
        self.parse = patch("bot.parse_workbook").start()
        self.draw = patch("bot.render_documents", return_value=(b"plan", b"invoices")).start()
        self.addCleanup(patch.stopall)
        self.start_bot()

    def start_bot(self, *, restricted=False):
        self.bot = Bot(self.store, "test", {51}, "https://mini.example", owner_id=42,
                       operator_ids={50} if restricted else ())
        self.bot.api = self.api

    def message(self, uid, *, elapsed=60, **fields):
        self.clock.return_value += elapsed
        self.bot.handle({"message": {"message_id": self.api.next_id(), "date": self.clock.return_value,
                         "from": {"id": uid}, "chat": {"id": uid, "type": "private"}, **fields}})

    def upload(self, uid, *rows):
        self.parse.return_value = list(rows)
        self.message(uid, document={"file_name": "orders.xlsx", "file_id": "orders"})

    def press(self, uid, prompt):
        button = prompt["reply_markup"]["inline_keyboard"][0][0]
        self.bot.handle({"callback_query": {"from": {"id": uid}, "id": "callback",
                         "message": {"chat": {"type": "private", "id": uid}, "message_id": prompt["message_id"]},
                         "data": button["callback_data"]}})

    def test_managed_membership_controls_intake_and_button(self):
        self.store.seed_staff(42, {51}, {50})
        self.start_bot(restricted=True)
        self.store.save_employee(52, "Новый сотрудник", [])
        self.store.save_employee(53, "Редактор", ["hours"])
        for uid, has_button in ((42, True), (50, False), (51, True), (52, False), (53, True)):
            with self.subTest(uid=uid):
                self.message(uid, text="/admin")
                self.assertEqual("reply_markup" in self.api.messages[-1], has_button)
                if not has_button:
                    self.assertIn("владельца", self.api.messages[-1]["text"])
        self.upload(52, receipt(1))
        self.assertEqual(self.store.get_job(52)["status"], "question")
        self.upload(51, receipt(2))
        pending = self.api.messages[-1]
        previous = next(s for s in self.store.staff_list() if s["id"] == "51")
        self.store.remove_employee(51, {"name": previous["name"], "permissions": previous["permissions"]})
        self.press(51, pending)
        self.message(51, text="/resume")
        self.assertIn("ограничен", self.api.messages[-1]["text"])
        self.upload(51, receipt(3))
        self.assertEqual(len(self.api.downloads), 2)
        self.assertEqual(self.store.get_job(51)["pending"]["message_id"], pending["message_id"])
        self.assertNotIn("суп", self.store.catalog()["dishes"])

    def test_id_is_private_and_available_before_enrollment(self):
        self.start_bot(restricted=True)
        self.message(7001, text="/id")
        self.assertEqual(self.api.messages[-1]["text"], "7001")
        self.assertNotIn("reply_markup", self.api.messages[-1])
        self.message(7001, text="/id", elapsed=1)
        self.assertEqual(len(self.api.messages), 1)
        self.message(7001, text="/id", chat={"type": "group", "id": -100})
        self.message(7001, text="/id", chat={"type": "private", "id": 7002})
        self.assertEqual(len(self.api.messages), 1)

        self.message(7002, text="/start")
        self.assertIn("7002", self.api.messages[-1]["text"])
        self.message(7002, text="/id", elapsed=1)
        self.assertEqual(len(self.api.messages), 2)
        self.message(7002, text="/id", elapsed=30)
        self.assertEqual(self.api.messages[-1]["text"], "7002")

    def test_unlisted_uploader_answers_apply_to_one_workbook_and_survive_resume(self):
        rows = [receipt(1, "Новая точка", "Блюдо Б"), receipt(2, "Новая точка", "Блюдо А")]
        self.upload(70, *rows)
        self.assertIn("только для этой выгрузки", self.api.messages[-1]["text"])
        self.message(70, text="Рабочий раздел")
        self.assertIn("Рабочий раздел", self.store.get_job(70)["pending"]["options"])
        self.message(70, text="РАБОЧИЙ РАЗДЕЛ")
        self.message(70, text="Улица 3")
        self.assertEqual(self.store.get_job(70)["status"], "done")
        catalog = self.draw.call_args.args[1]
        self.assertEqual(catalog["dishes"], {"блюдо а": "Рабочий раздел", "блюдо б": "Рабочий раздел"})
        self.assertEqual(catalog["points"]["новая точка"]["address"], "Улица 3")
        self.assertNotIn("Рабочий раздел", self.store.catalog()["sections"])
        self.assertFalse(self.store.catalog()["dishes"])
        self.assertFalse(self.store.catalog()["points"])
        self.store = Store(self.path)
        self.start_bot()
        self.message(70, text="/resume")
        self.assertEqual(self.draw.call_count, 2)
        self.assertEqual(self.draw.call_args.args[1], catalog)
        self.upload(71, *rows)
        self.assertEqual(self.store.get_job(71)["pending"]["name"], "Блюдо Б")
        self.upload(70, *rows)
        self.assertEqual(self.store.get_job(70)["pending"]["name"], "Блюдо Б")
        self.assertNotIn("Рабочий раздел", self.store.get_job(70)["pending"]["options"])

    def test_dish_editor_can_save_an_existing_section_but_new_sections_remain_local(self):
        self.store.add_section("Мой раздел")
        self.store.save_employee(60, "Редактор блюд", ["dishes"])
        self.upload(60, receipt(1, "Точка", "Блюдо А"), receipt(2, "Точка", "Блюдо Б"))
        self.message(60, text="МОЙ РАЗДЕЛ")
        self.message(60, text="Разовый раздел")
        self.press(60, self.api.messages[-1])  # Skip the address for this workbook.
        shared = self.store.catalog()
        self.assertEqual(shared["dishes"], {"блюдо а": "Мой раздел"})
        self.assertNotIn("Разовый раздел", shared["sections"])
        self.assertFalse(shared["points"])
        self.assertEqual(self.draw.call_args.args[1]["dishes"]["блюдо б"], "Разовый раздел")
        self.assertEqual(self.draw.call_args.args[1]["points"]["точка"]["address"], "")

    def test_local_address_fills_only_a_blank_shared_address_and_preserves_exclusion(self):
        self.store.save_new_dish("Суп", "Горячее")
        self.upload(70, receipt(1, "Точка"))
        self.store.save_new_point("Точка", "")
        self.store.edit_point("Точка", "", True)
        self.message(70, text="Адрес выгрузки")
        rendered = self.draw.call_args.args[1]["points"]["точка"]
        self.assertEqual((rendered["address"], rendered["excluded"]), ("Адрес выгрузки", True))
        self.assertEqual(self.store.catalog()["points"]["точка"]["address"], "")
        self.store.edit_point("Точка", "Адрес справочника", True)
        self.message(70, text="/resume")
        rendered = self.draw.call_args.args[1]["points"]["точка"]
        self.assertEqual((rendered["address"], rendered["excluded"]), ("Адрес справочника", True))

    def test_grants_updated_while_questions_are_open_apply_to_each_answer(self):
        self.store.save_employee(60, "Сотрудник", ["dishes", "sections"])
        self.upload(60, receipt(1, "Новая точка", "Блюдо"))
        previous = {"name": "Сотрудник", "permissions": ["dishes", "sections"]}
        self.store.save_employee(60, "Сотрудник", ["sections", "point_addresses"], previous)
        self.message(60, text="Разовый раздел")
        self.message(60, text="Новый адрес")
        shared = self.store.catalog()
        self.assertNotIn("блюдо", shared["dishes"])
        self.assertNotIn("Разовый раздел", shared["sections"])
        self.assertEqual(shared["points"]["новая точка"]["address"], "Новый адрес")
        self.upload(42, receipt(2, "Новая точка", "Блюдо"))
        self.message(42, text="Постоянный раздел")
        self.assertEqual(self.store.catalog()["dishes"]["блюдо"], "Постоянный раздел")
        self.message(60, text="/resume")
        self.assertEqual(self.draw.call_args.args[1]["dishes"]["блюдо"], "Постоянный раздел")


if __name__ == "__main__":
    unittest.main()
