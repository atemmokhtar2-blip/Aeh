import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import main


class ProgramExchangeRateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (main.DB_PATH, main.INITIAL_ADMINS, main.OWNER)
        main.DB_PATH = os.path.join(self.tmp.name, "rates.sqlite3")
        main.INITIAL_ADMINS = {123456}
        main.OWNER = 123456
        main.init_db()

    def tearDown(self):
        main.DB_PATH, main.INITIAL_ADMINS, main.OWNER = self.old
        self.tmp.cleanup()

    def test_each_program_has_its_own_default_rate(self):
        self.assertEqual(main.get_program_rate(1), 50.0)
        self.assertEqual(main.get_program_rate(2), 50.0)

    def test_changing_one_program_rate_does_not_change_another(self):
        with main.db() as conn:
            conn.execute("UPDATE programs SET exchange_rate='53.25' WHERE id=1")
        self.assertEqual(main.get_program_rate(1), 53.25)
        self.assertEqual(main.get_program_rate(2), 50.0)

    def test_program_rate_setting_button_is_shown(self):
        with main.db() as conn:
            p = conn.execute("SELECT id,name,code,exchange_rate FROM programs WHERE id=1").fetchone()
        keyboard = main.InlineKeyboardMarkup([
            [main.InlineKeyboardButton("تغيير سعر الصرف", callback_data=f"pe:rate:{p['id']}")]
        ])
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "pe:rate:1")

    async def test_admin_can_set_program_rate_from_message(self):
        user = SimpleNamespace(id=123456, username="admin", full_name="Admin")
        message = SimpleNamespace(text="61.75", reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_user=user,
                                 effective_chat=SimpleNamespace(type="private"))
        context = SimpleNamespace(user_data={"step": "editprogram_rate", "pid": 3})
        await main.messages(update, context)
        self.assertEqual(main.get_program_rate(3), 61.75)
        self.assertEqual(main.get_program_rate(4), 50.0)
        message.reply_text.assert_awaited_once()

    async def test_invalid_or_zero_rate_is_not_saved(self):
        user = SimpleNamespace(id=123456, username="admin", full_name="Admin")
        for value in ("0", "-5", "abc", "50.12345"):
            message = SimpleNamespace(text=value, reply_text=AsyncMock())
            update = SimpleNamespace(effective_message=message, effective_user=user,
                                     effective_chat=SimpleNamespace(type="private"))
            context = SimpleNamespace(user_data={"step": "editprogram_rate", "pid": 5})
            await main.messages(update, context)
            self.assertEqual(main.get_program_rate(5), 50.0)

    def test_request_rate_column_exists_for_rate_snapshot(self):
        with main.db() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(requests)")}
        self.assertIn("exchange_rate", columns)


if __name__ == "__main__":
    unittest.main()
