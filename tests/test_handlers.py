"""Real PTB dispatch with a local Telegram transport and the real CSV."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from telegram import Update
from telegram.ext import Application
from telegram.request import BaseRequest

from bot import build_application, post_init
from config import Config, ROOT
from db import Database
from handlers import Controller, AI, FEEDBACK, LIMITS
from sources import CsvSource


class OfflineTelegram(BaseRequest):
    def __init__(self):
        self.calls = []
        self.bot_user = {"id": 9000, "is_bot": True, "first_name": "Offline", "username": "offline_test_bot"}

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        action = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((action, params))
        if action == "getMe":
            result = self.bot_user
        elif action in ("setMyCommands", "answerCallbackQuery"):
            result = True
        elif action in ("sendMessage", "editMessageReplyMarkup"):
            result = {"message_id": len(self.calls), "date": 1, "from": self.bot_user,
                      "chat": {"id": int(params["chat_id"]), "type": "private"},
                      "text": params.get("text", "")}
        else:
            raise AssertionError("Unexpected Telegram method: " + action)
        return 200, json.dumps({"ok": True, "result": result}).encode()


class TelegramFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (ROOT / "runtime/tests").mkdir(parents=True, exist_ok=True)
        import asyncio
        asyncio.get_running_loop().slow_callback_duration = 2
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "runtime/tests")
        self.path = Path(self.temp.name) / "flow.db"
        self.config = Config(bot_token="123456:" + "x" * 30, db_path=self.path, admin_telegram_ids=frozenset({100}))
        self.db = Database(self.path)
        self.db.initialize()
        self.controller = Controller(self.config, self.db, CsvSource(ROOT / "data/ploswater.csv"))
        self.transport = OfflineTelegram()
        builder = Application.builder().request(self.transport).get_updates_request(self.transport)
        with patch("bot.Application.builder", return_value=builder):
            self.app = build_application(self.config, self.controller)
        await self.app.initialize()
        self.serial = 0
        self.errors = []
        async def capture_error(update, context):
            self.errors.append(type(context.error).__name__)
        self.app.add_error_handler(capture_error)

    async def asyncTearDown(self):
        await self.app.shutdown()
        self.temp.cleanup()
        self.assertEqual(self.errors, [], "Unexpected errors in real Telegram dispatch")

    async def send(self, text=None, callback=None, user_id=100, chat_type="private"):
        self.serial += 1
        user = {"id": user_id, "is_bot": False, "first_name": "Test"}
        message = {"message_id": self.serial, "date": 1, "from": user,
                   "chat": {"id": user_id, "type": chat_type}, "text": text or "Card"}
        if text and text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        raw = {"update_id": self.serial}
        if callback:
            raw["callback_query"] = {"id": str(self.serial), "from": user, "chat_instance": "offline",
                                     "data": callback, "message": message}
        else:
            raw["message"] = message
        before = len(self.transport.calls)
        with patch("socket.socket.connect", side_effect=AssertionError("Network is forbidden")):
            await self.app.process_update(Update.de_json(raw, self.app.bot))
        return self.transport.calls[before:]

    def messages(self, calls):
        return [params.get("text", "") for action, params in calls if action == "sendMessage"]

    def latest_search(self, user=100):
        with self.db.connect() as connection:
            return connection.execute("SELECT id FROM searches WHERE user_id=? ORDER BY rowid DESC", (user,)).fetchone()[0]

    async def test_registered_commands_and_future_features(self):
        await post_init(self.app)
        commands = next(params["commands"] for action, params in self.transport.calls if action == "setMyCommands")
        self.assertEqual(len(commands), 10)
        self.assertTrue(self.messages(await self.send("/start")))
        for text in ("/ai", AI, "/limits", LIMITS, "/feedback", FEEDBACK):
            output = self.messages(await self.send(text))
            self.assertTrue(any("Lab 2" in value or "Lab 3" in value for value in output))
        self.assertIn("/search", "\n".join(self.messages(await self.send("/help"))))
        self.assertIn("SQLite: проверка пройдена", "\n".join(self.messages(await self.send("/status"))))
        self.assertIn("Неизвестная команда", "\n".join(self.messages(await self.send("/unknown"))))

    async def test_full_search_paging_favorite_and_restart(self):
        await self.send("/start")
        await self.send("/search")
        await self.send("water")
        calls = await self.send(callback="form:run")
        cards = [params for action, params in calls if params.get("parse_mode") == "HTML"]
        self.assertEqual(len(cards), 5)
        search_id = self.latest_search()
        calls = await self.send(callback=f"p:{search_id}:1")
        self.assertEqual(sum(params.get("parse_mode") == "HTML" for _, params in calls), 5)
        await self.send(callback=f"f:a:{search_id}:0")
        item = self.db.result_item(100, search_id, 0)
        self.assertTrue(self.db.is_favorite(100, item.id))
        await self.app.shutdown()
        reopened = Database(self.path)
        self.db = reopened
        self.controller = Controller(self.config, reopened, CsvSource(ROOT / "data/ploswater.csv"))
        builder = Application.builder().request(self.transport).get_updates_request(self.transport)
        with patch("bot.Application.builder", return_value=builder):
            self.app = build_application(self.config, self.controller)
        await self.app.initialize()
        async def capture_error(update, context):
            self.errors.append(type(context.error).__name__)
        self.app.add_error_handler(capture_error)
        self.assertTrue(reopened.is_favorite(100, item.id))
        calls = await self.send(callback=f"p:{search_id}:0")
        self.assertEqual(sum(params.get("parse_mode") == "HTML" for _, params in calls), 5)
        calls = await self.send("/saved")
        self.assertEqual(sum(params.get("parse_mode") == "HTML" for _, params in calls), 1)
        saved_id = self.latest_search()
        await self.send(callback=f"f:r:{saved_id}:0")
        self.assertFalse(reopened.is_favorite(100, item.id))

    async def test_years_types_and_cancel(self):
        await self.send("/search")
        await self.send("climate change")
        await self.send(callback="form:years")
        self.assertIn("Некорректный диапазон", "\n".join(self.messages(await self.send("2025-2022"))))
        self.assertEqual(self.db.get_settings(100)[1], "years")
        await self.send("2022")
        calls = await self.send(callback="form:types")
        keyboard = next(params["reply_markup"] for action, params in calls if action == "sendMessage")
        self.assertTrue(all(isinstance(row[0], dict) for row in keyboard["inline_keyboard"]))
        types = sorted({m.material_type for m in self.controller.source.load() if m.material_type})
        await self.send(callback=f"type:{types.index('Review')}")
        await self.send(callback="form:run")
        search_id = self.latest_search()
        for _, item in self.db.get_page(100, search_id, 0):
            self.assertEqual(item.year, 2022)
            self.assertEqual(item.material_type, "Review")
        await self.send("/search")
        await self.send("/cancel")
        self.assertEqual(self.db.get_settings(100)[1], "idle")
        self.assertIn("Начните поиск", "\n".join(self.messages(await self.send("ignored input"))))

    async def test_admin_command_and_callback_both_check_permissions(self):
        self.assertIn("только администратору", "\n".join(self.messages(await self.send("/admin", user_id=200))))
        calls = await self.send(callback="admin:stats", user_id=200)
        self.assertEqual(self.messages(calls), [])
        denial = next(params for action, params in calls if action == "answerCallbackQuery")
        self.assertTrue(denial["show_alert"])
        self.assertIn("Доступ запрещён", denial["text"])
        self.assertIn("Диагностика", "\n".join(self.messages(await self.send("/admin"))))
        self.assertIn("пользователей", "\n".join(self.messages(await self.send(callback="admin:stats"))))
        self.assertIn("Lab 3", "\n".join(self.messages(await self.send(callback="admin:future"))))

    async def test_foreign_and_malformed_callback_is_rejected(self):
        await self.send("/search")
        await self.send("water")
        await self.send(callback="form:run")
        search_id = self.latest_search()
        for callback in (f"p:{search_id}:0", f"f:a:{search_id}:0"):
            self.assertIn("Подборка недоступна", "\n".join(self.messages(await self.send(callback=callback, user_id=200))))
        self.assertEqual(self.db.counts()["favorites"], 0)
        self.assertIn("Кнопка устарела", "\n".join(self.messages(await self.send(callback="p:invalid:x"))))

    async def test_private_chat_required(self):
        output = self.messages(await self.send("/search", chat_type="group"))
        self.assertIn("личный чат", "\n".join(output))
        self.assertEqual(self.db.counts()["users"], 0)

    async def test_unavailable_filter_does_not_change_settings(self):
        await self.send("/search")
        await self.send("water")
        before = self.db.get_settings(100)
        self.assertIn("не применяется", "\n".join(self.messages(await self.send(callback="future:open"))))
        self.assertEqual(before, self.db.get_settings(100))


if __name__ == "__main__":
    unittest.main()
