from threading import Event
import unittest
from unittest.mock import Mock, patch

from bot import Bot, COMMANDS, Telegram, TelegramError


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.store.staff_permissions.return_value = set()
        self.bot = Bot(self.store, "test", set(), "https://example.test", owner_id=42)
        self.bot.api = Mock()

    def send(self, uid, command):
        self.bot.handle({"message": {"date": 10, "message_id": 9, "chat": {"type": "private", "id": uid},
                                     "from": {"id": uid}, "text": command}})
        return self.bot.api.call.call_args.args[1]["text"]

    def test_help_is_scoped_and_preserves_current_job(self):
        owner_help = self.send(42, "/help")
        for command in COMMANDS:
            self.assertIn("/" + command, owner_help)
        self.assertIn("Статистика", owner_help)
        ordinary = self.send(78, "/help")
        self.assertIn("/id", ordinary)
        self.assertNotIn("/admin", ordinary)
        self.store.staff_permissions.return_value = {"hours"}
        self.assertIn("/admin", self.send(78, "/"))
        self.assertIn("/help", self.send(78, "/other"))
        self.store.get_job.assert_not_called()
        self.store.update_job.assert_not_called()

    def test_private_command_registration_retries_without_stopping_polling(self):
        stop = Event()
        registered = []
        polls = []
        clock = [0]
        def call(method, fields):
            if method == "setMyCommands":
                registered.append(fields)
                if len(registered) == 1:
                    raise TelegramError(429, 600)
            elif method == "getUpdates":
                polls.append(clock[0])
                clock[0] += 300
                if len(polls) == 4:
                    stop.set()
                return []
            return True
        self.bot.api.call.side_effect = call
        with patch("bot.monotonic", side_effect=lambda: clock[0]), self.assertLogs("bot", level="WARNING"):
            self.bot.run(stop)
        self.assertEqual([0, 300, 600, 900], polls)
        self.assertEqual(2, len(registered))
        self.assertEqual({"type": "all_private_chats"}, registered[-1]["scope"])
        self.assertEqual(set(COMMANDS), {command["command"] for command in registered[-1]["commands"]})

    def test_menu_has_a_short_timeout_during_railway_startup(self):
        with patch("bot.urlopen", side_effect=TimeoutError) as network:
            with self.assertRaises(TelegramError):
                Telegram("test").call("setMyCommands", {})
        self.assertLessEqual(network.call_args.kwargs["timeout"], 5)
