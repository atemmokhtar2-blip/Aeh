import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
