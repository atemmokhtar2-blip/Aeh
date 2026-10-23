import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import main


class PerProgramExchangeRateTests(unittest.IsolatedAsyncioTestCase):
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

    def test_admin_home_has_no_global_exchange_rate_button(self):
        callbacks = [
            button.callback_data
            for row in main.admin_kb().inline_keyboard
            for button in row
        ]
        self.assertNotIn("a:rate", callbacks)
        self.assertIn("a:programs", callbacks)

    async def test_program_settings_exposes_its_own_rate_button(self):
        q = SimpleNamespace(
            data="pe:1",
            from_user=SimpleNamespace(id=123456),
            message=SimpleNamespace(chat=SimpleNamespace(type="private"), chat_id=123456, reply_text=AsyncMock()),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=q)
        context = SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=AsyncMock()))
        await main.callbacks(update, context)
        keyboard = q.edit_message_text.await_args.kwargs["reply_markup"]
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn("pe:rate:1", callbacks)

    async def test_rate_update_changes_only_selected_program(self):
        user = SimpleNamespace(id=123456, username="admin", full_name="Admin")
        message = SimpleNamespace(text="62.5", reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="private"),
            effective_user=user,
            effective_message=message,
            message=message,
        )
        context = SimpleNamespace(user_data={"step": "editprogram_rate", "pid": 3})
        await main.messages(update, context)
        self.assertEqual(main.get_program_rate(3), 62.5)
        self.assertEqual(main.get_program_rate(2), 50.0)
        self.assertNotIn("step", context.user_data)

    async def test_invalid_program_rate_does_not_change_rate(self):
        user = SimpleNamespace(id=123456, username="admin", full_name="Admin")
        message = SimpleNamespace(text="0", reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="private"),
            effective_user=user,
            effective_message=message,
            message=message,
        )
        context = SimpleNamespace(user_data={"step": "editprogram_rate", "pid": 4})
        await main.messages(update, context)
        self.assertEqual(main.get_program_rate(4), 50.0)
        message.reply_text.assert_awaited_once()

    def test_program_rate_is_persistent_in_database(self):
        with main.db() as conn:
            conn.execute("UPDATE programs SET exchange_rate=? WHERE id=?", ("73.25", 5))
        main.init_db()
        self.assertEqual(main.get_program_rate(5), 73.25)


if __name__ == "__main__":
    unittest.main()
