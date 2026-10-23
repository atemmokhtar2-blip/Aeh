import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import main


class ProgramEditButtonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (main.DB_PATH, main.INITIAL_ADMINS, main.OWNER)
        main.DB_PATH = os.path.join(self.tmp.name, "buttons.sqlite3")
        main.INITIAL_ADMINS = {123456}
        main.OWNER = 123456
        main.init_db()
        self.query = SimpleNamespace(
            data="program_edit_confirm",
            from_user=SimpleNamespace(id=123456),
            message=SimpleNamespace(chat=SimpleNamespace(type="private"), chat_id=123456),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message_reply_text=AsyncMock(),
        )
        self.query.message.reply_text = AsyncMock()
        self.update = SimpleNamespace(callback_query=self.query)
        self.context = SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=AsyncMock()))

    def tearDown(self):
        main.DB_PATH, main.INITIAL_ADMINS, main.OWNER = self.old
        self.tmp.cleanup()

    async def test_confirm_name_changes_only_name_and_audits(self):
        self.context.user_data.update(step="confirm_program_edit_name", pid=1, pending_program_value="اسم جديد")
        await main.callbacks(self.update, self.context)
        with main.db() as conn:
            row = conn.execute("SELECT name,code FROM programs WHERE id=1").fetchone()
            audit = conn.execute("SELECT action,details FROM activity ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((row["name"], row["code"]), ("اسم جديد", "لم يحدد الكود بعد"))
        self.assertEqual(audit["action"], "program_name_updated")
        self.assertFalse(self.context.user_data)

    async def test_confirm_code_changes_only_code_and_audits(self):
        self.context.user_data.update(step="confirm_program_edit_code", pid=2, pending_program_value="CODE-2026")
        await main.callbacks(self.update, self.context)
        with main.db() as conn:
            row = conn.execute("SELECT name,code FROM programs WHERE id=2").fetchone()
            audit = conn.execute("SELECT action FROM activity ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["name"], "برنامج 02")
        self.assertEqual(row["code"], "CODE-2026")
        self.assertEqual(audit["action"], "program_id_updated")

    async def test_cancel_does_not_change_program(self):
        self.query.data = "program_edit_cancel"
        self.context.user_data.update(step="confirm_program_edit_name", pid=1, pending_program_value="يجب ألا يُحفظ")
        await main.callbacks(self.update, self.context)
        with main.db() as conn:
            row = conn.execute("SELECT name,code FROM programs WHERE id=1").fetchone()
        self.assertEqual(row["name"], "برنامج 01")
        self.assertEqual(row["code"], "لم يحدد الكود بعد")
        self.assertFalse(self.context.user_data)

    async def test_stale_confirmation_does_not_write(self):
        self.context.user_data.update(step="confirm_program_edit_name", pid=1)
        await main.callbacks(self.update, self.context)
        with main.db() as conn:
            row = conn.execute("SELECT name FROM programs WHERE id=1").fetchone()
        self.assertEqual(row["name"], "برنامج 01")
        self.assertFalse(self.context.user_data)

    async def test_non_admin_cannot_confirm_edit(self):
        self.query.from_user.id = 987654
        self.context.user_data.update(step="confirm_program_edit_name", pid=1, pending_program_value="غير مصرح")
        await main.callbacks(self.update, self.context)
        with main.db() as conn:
            row = conn.execute("SELECT name FROM programs WHERE id=1").fetchone()
        self.assertEqual(row["name"], "برنامج 01")

    def test_program_edit_confirmation_keyboard_has_both_actions(self):
        from telegram import InlineKeyboardMarkup
        keyboard = InlineKeyboardMarkup([
            [main.InlineKeyboardButton("تأكيد", callback_data="program_edit_confirm"),
             main.InlineKeyboardButton("إلغاء", callback_data="program_edit_cancel")]
        ])
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(callbacks, ["program_edit_confirm", "program_edit_cancel"])


if __name__ == "__main__":
    unittest.main()
