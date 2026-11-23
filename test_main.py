from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from aiogram.types import Chat, User

import main


async def _record_position(positions: list[int], position: int) -> None:
    positions.append(position)


class TokenBudgetTests(unittest.IsolatedAsyncioTestCase):
    def test_account_view_includes_user_chat_and_usage(self) -> None:
        user = User(id=12345, is_bot=False, first_name="Тест", username="tester")
        chat = Chat(id=-1001234567890, type="supergroup", title="Тестовая группа")
        gigachat = SimpleNamespace(model="GigaChat", tokens_used=518)
        conversations = {chat.id: [{"role": "system", "content": "prompt"}, {"role": "user", "content": "hi"}]}

        text = main.account_view("overview", user, chat, gigachat, conversations)

        self.assertIn("Telegram ID: 12345", text)
        self.assertIn("Chat ID: -1001234567890", text)
        self.assertIn("Тестовая группа (supergroup)", text)
        self.assertIn("Сообщений в контексте: 1", text)
        self.assertIn("GigaChat", text)
        self.assertIn("518 токенов", text)
        self.assertIn("внутренний лимит выключен", text)
        callbacks = [button.callback_data for row in main.account_keyboard().inline_keyboard for button in row]
        self.assertEqual(
            callbacks,
            ["lk:overview", "lk:profile", "lk:chat", "lk:usage", "lk:price", "lk:reset"],
        )

    async def test_second_request_is_reported_in_queue(self) -> None:
        chat_started = asyncio.Event()
        release_first = asyncio.Event()
        queued_positions: list[int] = []
        chat_calls = 0

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal chat_calls
            if request.url.path.endswith("/oauth"):
                return httpx.Response(
                    200,
                    json={"access_token": "test-token", "expires_at": int((time.time() + 3600) * 1000)},
                )
            chat_calls += 1
            if chat_calls == 1:
                chat_started.set()
                await release_first.wait()
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 10}},
            )

        with patch.dict(os.environ, {"GIGACHAT_AUTH_KEY": "test-key"}):
            client = main.GigaChatClient()
        with tempfile.TemporaryDirectory() as directory:
            client.usage_path = Path(directory) / "usage.json"
            await client.close()
            client.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            try:
                first = asyncio.create_task(client.complete([{"role": "user", "content": "first"}]))
                await chat_started.wait()
                second = asyncio.create_task(
                    client.complete(
                        [{"role": "user", "content": "second"}],
                        on_queued=lambda position: _record_position(queued_positions, position),
                    )
                )
                await asyncio.sleep(0)
                self.assertEqual(queued_positions, [1])
                self.assertEqual(chat_calls, 1)
                release_first.set()
                await asyncio.gather(first, second)
                self.assertEqual(chat_calls, 2)
                self.assertEqual(client.queued_requests, 0)
            finally:
                release_first.set()
                await client.close()

    async def test_usage_is_counted_without_a_bot_budget(self) -> None:
        calls: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if request.url.path.endswith("/oauth"):
                return httpx.Response(
                    200,
                    json={
                        "access_token": "test-token",
                        "expires_at": int((time.time() + 3600) * 1000),
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "Тестовый ответ"}}],
                    "usage": {"total_tokens": 300 if len(calls) == 2 else 200},
                },
            )

        with patch.dict(os.environ, {"GIGACHAT_AUTH_KEY": "test-key"}):
            client = main.GigaChatClient()
        with tempfile.TemporaryDirectory() as directory:
            client.usage_path = Path(directory) / "usage.json"
            await client.close()
            client.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            try:
                messages = [{"role": "user", "content": "test"}]
                self.assertEqual(await client.complete(messages), "Тестовый ответ")
                self.assertEqual(await client.complete(messages), "Тестовый ответ")
                self.assertEqual(await client.complete(messages), "Тестовый ответ")

                chat_calls = [request for request in calls if request.url.path.endswith("/chat/completions")]
                self.assertEqual(len(chat_calls), 3)
                self.assertTrue(all("max_tokens" not in json.loads(request.content) for request in chat_calls))
                self.assertEqual(json.loads(client.usage_path.read_text())["tokens_used"], 700)
            finally:
                await client.close()


class AgentModeTests(unittest.TestCase):
    def test_mode_prompt_distinguishes_llm_and_agi_prototype(self) -> None:
        self.assertEqual(main.mode_prompt("llm", "Base prompt"), "Base prompt")
        agi_prompt = main.mode_prompt("agi", "Base prompt")
        self.assertIn("AGI-прототипа", agi_prompt)
        self.assertIn("Не утверждай", agi_prompt)

    def test_mode_keyboard_offers_both_modes(self) -> None:
        buttons = [button for row in main.mode_keyboard("llm").inline_keyboard for button in row]
        self.assertEqual([button.callback_data for button in buttons], ["mode:llm", "mode:agi"])

    def test_admin_ids_are_parsed_and_invalid_values_ignored(self) -> None:
        self.assertEqual(main.parse_admin_ids("123, bad, 456 "), {123, 456})

    def test_request_throttle_allows_after_cooldown(self) -> None:
        timestamps: dict[int, float] = {}
        self.assertTrue(main.allow_request(123, timestamps, 10.0))
        self.assertFalse(main.allow_request(123, timestamps, 11.0))
        self.assertTrue(main.allow_request(123, timestamps, 12.1))

    def test_agent_plan_generates_goal_and_steps(self) -> None:
        plan = main.build_agent_plan("Нужно собрать MVP-бота за 2 дня")
        self.assertIn("Нужно собрать MVP-бота за 2 дня", plan["goal"])
        self.assertGreaterEqual(len(plan["steps"]), 3)
        self.assertIn("действия", plan["summary"].lower())

    def test_agent_tool_catalog_has_business_items(self) -> None:
        pricing = main.execute_agent_tool("price_catalog", {})
        self.assertIn("AGI-бот", pricing)
        self.assertIn("Rust", pricing)
        self.assertIn("от", pricing.lower())

    def test_agent_status_lists_tools_and_mode(self) -> None:
        status = main.agent_status("demo-chat", "prototype")
        self.assertIn("prototype", status.lower())
        self.assertIn("price_catalog", status)
        self.assertIn("calculator", status)


class PricingAndSpamTests(unittest.TestCase):
    def test_price_catalog_contains_service_entries(self) -> None:
        catalog = main.price_catalog_text()
        self.assertIn("299", catalog)
        self.assertIn("5 млн", catalog)
        self.assertIn("Оплатить", catalog)
        self.assertIn("Старт", catalog)

    def test_price_flow_has_no_back_buttons_on_catalog(self) -> None:
        keyboard = main.price_keyboard()
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertNotIn("Назад", labels)
        self.assertNotIn("Назад к тарифам", labels)

    def test_chat_cancel_is_blocked_while_processing(self) -> None:
        busy_chats: set[int] = set()
        busy_chats.add(42)
        self.assertTrue(main.is_chat_busy(42, busy_chats))
        self.assertFalse(main.is_chat_busy(43, busy_chats))


if __name__ == "__main__":
    unittest.main()