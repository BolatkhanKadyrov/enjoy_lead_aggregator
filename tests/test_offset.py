from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from store import Store


class OffsetTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = Path(temp.name) / "offset.sqlite"
        self.store = Store(self.database)

    def test_idle_polls_do_not_extend_cursor_lifetime(self):
        saved = 1_790_000_000
        with patch("store.time.time", return_value=saved):
            self.store.set_offset(900_000)
        with patch("store.time.time", return_value=saved + 47 * 3600):
            self.assertEqual(Store(self.database).get_offset(), 900_000)
        with patch("store.time.time", return_value=saved + 48 * 3600):
            self.assertEqual(self.store.get_offset(), 0)
        with patch("store.time.time", return_value=saved + 8 * 24 * 3600):
            self.assertEqual(self.store.get_offset(), 0)
            self.store.set_offset(45)
            self.assertEqual(self.store.get_offset(), 45)

    def test_undated_cursor_from_existing_database(self):
        with self.store.connection(write=True) as conn:
            conn.execute("UPDATE config SET value='81234' WHERE key='offset'")
        reopened = Store(self.database)
        self.assertEqual(reopened.get_offset(), 0)
        reopened.set_offset(19)
        self.assertEqual(reopened.get_offset(), 19)
