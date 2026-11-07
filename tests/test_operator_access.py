import unittest
from unittest.mock import Mock, call

from bot import Bot
from main import telegram_ids
from store import Store


def message(uid, **fields):
    return {"chat": {"id": uid, "type": "private"}, "from": {"id": uid},
            "date": 1000, "message_id": 12, **fields}


class OperatorAccessTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock(spec=Store)
        self.store.settings.return_value = {"start": "18:00", "end": "23:30"}
        self.bot = Bot(self.store, "test", {101}, "https://example.invalid", operator_ids={102})
        self.bot.api = Mock()

    def test_unlisted_account_cannot_upload_resume_or_answer_an_old_question(self):
        self.store.should_notify.side_effect = [True, False, False]
        for fields in ({"document": {"file_id": "test", "file_name": "orders.xlsx"}},
                       {"text": "/resume"}, {"text": "Адрес из предыдущего вопроса"}):
            self.bot.handle({"message": message(103, **fields)})
        self.bot.handle({"callback_query": {"id": "query", "from": {"id": 103},
                                            "message": message(103), "data": "obsolete:skip"}})
        self.assertEqual(self.store.mock_calls, [call.should_notify(103, 1000, album=None)] * 3)
        self.bot.api.download.assert_not_called()
        self.bot.api.call.assert_called_once()
        method, data = self.bot.api.call.call_args.args
        self.assertEqual((method, data["chat_id"]), ("sendMessage", 103))
        self.assertIn("Доступ", data["text"])

    def test_operator_and_admin_enter_and_only_admin_sees_settings_button(self):
        for uid in (101, 102):
            self.bot.handle({"message": message(uid, text="/admin")})
            method, data = self.bot.api.call.call_args.args
            self.assertEqual((method, data["chat_id"]), ("sendMessage", uid))
            self.assertEqual("reply_markup" in data, uid == 101)
        self.store.should_notify.assert_not_called()

    def test_empty_operator_list_preserves_open_mode(self):
        bot = Bot(self.store, "test", {101}, "", operator_ids=telegram_ids("  ", "OPERATOR_IDS"))
        bot.api = Mock()
        bot.handle({"message": message(103, text="/start")})
        self.store.settings.assert_called_once_with()
        self.store.should_notify.assert_not_called()

    def test_invalid_operator_configuration_fails_closed(self):
        self.assertEqual(telegram_ids("12, 21\n21", "OPERATOR_IDS"), {12, 21})
        for value in ("12, abc", "-1", "0", ",,"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "OPERATOR_IDS"):
                telegram_ids(value, "OPERATOR_IDS")
