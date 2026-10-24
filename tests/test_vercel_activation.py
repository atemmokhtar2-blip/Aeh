import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import main
from api import index as webhook


class VercelActivationNoticeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_db_path = main.DB_PATH
        self.previous_database_url = main.DATABASE_URL
        self.previous_use_postgres = main._USE_POSTGRES
        self.previous_admins = main.INITIAL_ADMINS
        self.previous_owner = main.OWNER
        self.previous_app = webhook.telegram_app

        main.DB_PATH = os.path.join(self.tmp.name, "activation.sqlite3")
        main.DATABASE_URL = ""
        main._USE_POSTGRES = False
        main.INITIAL_ADMINS = {111111, 222222}
        main.OWNER = 111111
        main.init_db()
        webhook.telegram_app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

    def tearDown(self):
        webhook.telegram_app = self.previous_app
        main.DB_PATH = self.previous_db_path
        main.DATABASE_URL = self.previous_database_url
        main._USE_POSTGRES = self.previous_use_postgres
        main.INITIAL_ADMINS = self.previous_admins
        main.OWNER = self.previous_owner
        self.tmp.cleanup()

    async def test_activation_notice_is_sent_to_each_admin_once_per_deployment(self):
        await webhook.notify_admins_activated("deployment-test")
        await webhook.notify_admins_activated("deployment-test")

        send_message = webhook.telegram_app.bot.send_message
        self.assertEqual(send_message.await_count, 2)
        self.assertEqual(
            {call.kwargs["chat_id"] for call in send_message.await_args_list},
            {111111, 222222},
        )
        self.assertEqual(
            {call.kwargs["text"] for call in send_message.await_args_list},
            {"تم تفعيل البوت بنجاح."},
        )
        with main.db() as conn:
            rows = conn.execute(
                "SELECT value FROM settings WHERE key LIKE 'vercel_activation_notice:%'"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["value"] == "sent" for row in rows))


if __name__ == "__main__":
    unittest.main()
