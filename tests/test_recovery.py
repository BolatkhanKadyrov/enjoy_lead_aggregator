import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

from bot import Bot, TelegramError
from store import Store


class RecoveryTests(unittest.TestCase):
    def test_typed_address_fills_skipped_point_and_preserves_admin_changes(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "orders.sqlite")
            store.save_new_point("Кофейня", "")
            store.edit_point("Кофейня", "", True)
            store.save_new_point("кофейня", "Улица 12")
            store.save_new_point("КОФЕЙНЯ", "Другой адрес")
            store.save_new_point("Кофейня", "")
            self.assertEqual(store.catalog()["points"]["кофейня"],
                             {"name": "Кофейня", "address": "Улица 12", "excluded": True})

    def test_readiness_recovers_only_after_successful_poll_and_cursor_write(self):
        store = Mock()
        store.get_offset.return_value = 10
        store.set_offset.side_effect = [sqlite3.OperationalError("storage unavailable"), None]
        bot = Bot(store, "test", set(), "")
        stop = Event()
        update = {"update_id": 10}
        replies = iter([[], TelegramError(503), [update], [update]])
        poll_readiness = []
        handler_readiness = []
        retry_readiness = []

        def poll(method, fields):
            if method in ("deleteWebhook", "setMyCommands"):
                return True
            poll_readiness.append(bot.connected)
            reply = next(replies, None)
            if reply is None:
                stop.set()
                return []
            if isinstance(reply, TelegramError):
                raise reply
            return reply

        bot.api.call = poll
        bot.handle = lambda _: handler_readiness.append(bot.connected)
        with patch.object(stop, "wait", side_effect=lambda _: retry_readiness.append(bot.connected)), \
                self.assertLogs("bot", level="WARNING"):
            bot.run(stop)
        self.assertEqual(poll_readiness, [False, True, False, False, True])
        self.assertEqual(handler_readiness, [False, False])
        self.assertEqual(retry_readiness, [False, False])
        self.assertFalse(bot.connected)
