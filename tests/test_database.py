import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import main


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        main.DB_PATH = os.path.join(self.tmp.name, "test.sqlite3")
        main.INITIAL_ADMINS = {123456}
        main.OWNER = 123456
        main.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_initializes_ten_program_slots(self):
        self.assertEqual(len(main.get_programs()), 10)

    def test_admin_is_registered(self):
        self.assertTrue(main.is_admin(123456))
        self.assertFalse(main.is_admin(654321))

    def test_programs_can_be_disabled(self):
        with main.db() as conn:
            conn.execute("UPDATE programs SET enabled=0 WHERE id=1")
        self.assertEqual(len(main.get_programs(active=True)), 9)

    def test_removed_admin_is_not_restored_on_restart(self):
        with main.db() as conn:
            conn.execute("INSERT OR IGNORE INTO admins VALUES(?,?,?)", (654321, 123456, main.now()))
            conn.execute("DELETE FROM admins WHERE user_id=?", (654321,))
        main.init_db()
        self.assertFalse(main.is_admin(654321))

    def test_owner_remains_registered(self):
        with main.db() as conn:
            conn.execute("DELETE FROM admins WHERE user_id=?", (123456,))
        main.init_db()
        self.assertTrue(main.is_admin(123456))

    def test_welcome_photo_sent_flag_persists(self):
        with main.db() as conn:
            conn.execute("INSERT INTO bot_users(user_id,username,full_name,first_seen,last_seen) VALUES(?,?,?,?,?)", (123456, "tester", "Test User", main.now(), main.now()))
            row = conn.execute("SELECT welcome_photo_sent FROM bot_users WHERE user_id=123456").fetchone()
            self.assertEqual(row["welcome_photo_sent"], 0)
            conn.execute("UPDATE bot_users SET welcome_photo_sent=1 WHERE user_id=123456")
        main.init_db()
        with main.db() as conn:
            row = conn.execute("SELECT welcome_photo_sent FROM bot_users WHERE user_id=123456").fetchone()
        self.assertEqual(row["welcome_photo_sent"], 1)

    def test_migrates_existing_bot_users_for_welcome_photo(self):
        with main.db() as conn:
            conn.execute("DROP TABLE bot_users")
            conn.execute("CREATE TABLE bot_users(user_id INTEGER PRIMARY KEY,username TEXT,full_name TEXT,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL)")
            conn.execute("INSERT INTO bot_users VALUES(?,?,?,?,?)", (987654, "olduser", "Old User", main.now(), main.now()))
        main.init_db()
        with main.db() as conn:
            row = conn.execute("SELECT welcome_photo_sent,username FROM bot_users WHERE user_id=987654").fetchone()
        self.assertEqual(row["welcome_photo_sent"], 0)
        self.assertEqual(row["username"], "olduser")

    def test_csv_formula_payload_is_neutralized(self):
        self.assertEqual(main.csv_safe("=1+1"), "'=1+1")
        self.assertEqual(main.csv_safe("@SUM(A1:A2)"), "'@SUM(A1:A2)")
        self.assertEqual(main.csv_safe("normal wallet"), "normal wallet")

    def test_activity_log_persists(self):
        main.audit(123456, "test_action", "unit test")
        with main.db() as conn:
            row = conn.execute("SELECT action,details FROM activity ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["action"], "test_action")
        self.assertEqual(row["details"], "unit test")


class StartWelcomeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_db_path = main.DB_PATH
        self.previous_admins = main.INITIAL_ADMINS
        self.previous_owner = main.OWNER
        self.previous_photo_path = main.WELCOME_PHOTO_PATH
        main.DB_PATH = os.path.join(self.tmp.name, "start.sqlite3")
        main.INITIAL_ADMINS = set()
        main.OWNER = None
        main.WELCOME_PHOTO_PATH = Path(self.tmp.name) / "welcome.jpg"
        main.WELCOME_PHOTO_PATH.write_bytes(b"test-image")
        main.init_db()

    def tearDown(self):
        main.DB_PATH = self.previous_db_path
        main.INITIAL_ADMINS = self.previous_admins
        main.OWNER = self.previous_owner
        main.WELCOME_PHOTO_PATH = self.previous_photo_path
        self.tmp.cleanup()

    async def test_start_sends_welcome_photo_only_once(self):
        user = SimpleNamespace(id=987654, username="guest", full_name="Guest")
        message = SimpleNamespace(reply_photo=AsyncMock(), reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="private"),
            effective_user=user,
            message=message,
        )
        context = SimpleNamespace(user_data={})

        await main.start(update, context)
        await main.start(update, context)

        self.assertEqual(message.reply_photo.await_count, 2)
        self.assertEqual(message.reply_text.await_count, 0)
        with main.db() as conn:
            row = conn.execute("SELECT user_id FROM bot_users WHERE user_id=?", (user.id,)).fetchone()
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
