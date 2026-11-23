from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

import httpx
from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, User
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

TOKEN_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
logger = logging.getLogger("gigachat_bot")
TOKEN_BUDGET: int | None = None
REQUEST_COOLDOWN_SECONDS = 2.0

AGENT_TOOLS: dict[str, str] = {
    "calculator": "Считает арифметические выражения и пропускает промежуточные вычисления.",
    "price_catalog": "Возвращает прайс-лист для гонораров и услуг: AGI-бот, Rust-модернизация, админка.",
    "task_planner": "Строит краткий plan → steps → risks → result для ваших задач.",
    "memory": "Показывает текущий стек целей, планов и заметок для этого чата.",
}


def build_agent_plan(goal: str) -> dict[str, object]:
    normalized_goal = (goal or "Нужно сформировать рабочий план").strip()
    steps = [
        "Определить цель, критерии успеха и ограничения.",
        "Разбить задачу на приоритетные этапы и ответственные блоки.",
        "Проверить риски, зависимости и технические ограничения.",
        "Выполнить минимальный рабочий вариант и собрать результат для проверки.",
    ]
    summary = (
        f"План действия для: {normalized_goal}. "
        "Сначала фиксируем цель, затем разбиваем задачу на этапы, проверяем риски и делаем "
        "минимально рабочую версию."
    )
    return {"goal": normalized_goal, "steps": steps, "summary": summary}


def execute_agent_tool(tool_name: str, payload: dict[str, object] | None = None) -> str:
    payload = payload or {}
    tool_name = (tool_name or "").strip().lower()
    if tool_name == "calculator":
        expression = str(payload.get("expression", "")).strip()
        if not expression:
            return "Формула пуста. Пример: calculator {expression: '2+2*3'}"
        try:
            result = eval(expression, {"__builtins__": {}}, {"abs": abs, "round": round, "min": min, "max": max})
            return f"Результат: {result}"
        except Exception:
            return "Не удалось вычислить выражение. Используйте базовую арифметику вроде 2+2*3."
    if tool_name == "price_catalog":
        return (
            "Прайс-лист AGI-проекта\n"
            "- AGI-бот: от 25 000 ₽\n"
            "- Rust-модернизация: от 35 000 ₽\n"
            "- Админка и ЛК: от 15 000 ₽\n"
            "- Лимиты / биллинг / каталог: от 12 000 ₽\n"
            "- Full-stack AGI-платформа: от 80 000 ₽"
        )
    if tool_name == "task_planner":
        goal = str(payload.get("goal", "Нужно подготовить рабочий план")).strip()
        plan = build_agent_plan(goal)
        steps = "\n".join(f"{index + 1}. {step}" for index, step in enumerate(plan["steps"]))
        return f"Цель: {plan['goal']}\n\nПлан:\n{steps}"
    if tool_name == "memory":
        goal = str(payload.get("goal", "текущая сессия")).strip() or "текущая сессия"
        return (
            f"Память AGI: {goal}\n"
            "- цель: зафиксирована\n"
            "- состояние: режим прототипа\n"
            "- инструменты: calculator, price_catalog, task_planner, memory"
        )
    return (
        "Неизвестный инструмент. Доступно: " + ", ".join(AGENT_TOOLS)
        + ". Для вызова используйте /tools или /plan <цель>."
    )


def agent_status(chat_id: int | str, mode: str) -> str:
    tools = ", ".join(AGENT_TOOLS)
    return (
        "AGI-статус\n"
        f"Chat ID: {chat_id}\n"
        f"Режим: {mode}\n"
        f"Инструменты: {tools}\n"
        "Команды: /agent, /plan <задача>, /tools"
    )


def parse_admin_ids(value: str) -> set[int]:
    admin_ids: set[int] = set()
    for raw_id in value.split(","):
        raw_id = raw_id.strip()
        if raw_id.isdigit():
            admin_ids.add(int(raw_id))
    return admin_ids


def allow_request(user_id: int, request_times: dict[int, float], now: float) -> bool:
    for stored_user_id, last_request in list(request_times.items()):
        if now - last_request > 60:
            del request_times[stored_user_id]
    previous_request = request_times.get(user_id)
    if previous_request is not None and now - previous_request < REQUEST_COOLDOWN_SECONDS:
        return False
    request_times[user_id] = now
    return True


def mode_keyboard(mode: str) -> InlineKeyboardMarkup:
    llm_label = "✓ LLM" if mode == "llm" else "LLM"
    agi_label = "✓ AGI" if mode == "agi" else "AGI"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=llm_label, callback_data="mode:llm"),
                InlineKeyboardButton(text=agi_label, callback_data="mode:agi"),
            ]
        ]
    )


def mode_prompt(mode: str, system_prompt: str) -> str:
    if mode == "agi":
        return (
            f"{system_prompt}\n\n"
            "Режим AGI-прототипа: для запроса уточняй цель и ограничения, предлагай план, "
            "учитывай риски и формулируй проверяемый результат. Не утверждай, что выполнял "
            "действия вне чата или запускал инструменты, если этого не происходило."
        )
    return system_prompt


class GigaChatClient:
    def __init__(self) -> None:
        auth_key = os.getenv("GIGACHAT_AUTH_KEY", "").strip()
        if not auth_key:
            raise ValueError("Заполните GIGACHAT_AUTH_KEY в файле .env")
        self.auth_key = auth_key if auth_key.lower().startswith("basic ") else f"Basic {auth_key}"
        self.scope = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()
        self.model = os.getenv("GIGACHAT_MODEL", "GigaChat").strip()
        self.client = httpx.AsyncClient(timeout=60.0, verify=ssl.create_default_context())
        self.access_token: str | None = None
        self.token_expires_at = 0.0
        self.usage_path = ROOT / "usage.json"
        try:
            self.tokens_used = int(json.loads(self.usage_path.read_text()).get("tokens_used", 0))
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            self.tokens_used = 0
        self.usage_lock = asyncio.Lock()
        self.queue_lock = asyncio.Lock()
        self.queued_requests = 0

    async def close(self) -> None:
        await self.client.aclose()

    async def _get_access_token(self) -> str:
        if self.access_token and time.time() < self.token_expires_at - 60:
            return self.access_token

        response = await self.client.post(
            TOKEN_URL,
            headers={
                "Authorization": self.auth_key,
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"scope": self.scope},
        )
        response.raise_for_status()
        payload = response.json()
        self.access_token = payload["access_token"]
        self.token_expires_at = int(payload.get("expires_at", 0)) / 1000 or time.time() + 1500
        return self.access_token

    async def complete(
        self,
        messages: list[dict[str, str]],
        on_queued: Callable[[int], Awaitable[None]] | None = None,
        on_started: Callable[[], Awaitable[None]] | None = None,
    ) -> str:
        queued = False
        queue_position = 0
        if self.usage_lock.locked():
            async with self.queue_lock:
                queued = self.usage_lock.locked()
                if queued:
                    self.queued_requests += 1
                    queue_position = self.queued_requests
            if queued and on_queued:
                await on_queued(queue_position)

        try:
            async with self.usage_lock:
                if queued:
                    async with self.queue_lock:
                        self.queued_requests -= 1
                    queued = False
                if on_started:
                    await on_started()

                token = await self._get_access_token()
                request_body: dict[str, object] = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.7,
                }
                if TOKEN_BUDGET is not None:
                    remaining = max(0, TOKEN_BUDGET - self.tokens_used)
                    if remaining <= 0:
                        raise RuntimeError("Configured token budget is exhausted")
                    request_body["max_tokens"] = remaining

                response = await self.client.post(
                    CHAT_URL,
                    headers={"Authorization": f"Bearer {token}"},
                    json=request_body,
                )
                response.raise_for_status()
                payload = response.json()
                usage = payload.get("usage") or {}
                tokens_used = int(usage.get("total_tokens") or 0)
                self.tokens_used += tokens_used
                temporary_path = self.usage_path.with_suffix(".tmp")
                temporary_path.write_text(json.dumps({"tokens_used": self.tokens_used}))
                temporary_path.replace(self.usage_path)
                return payload["choices"][0]["message"]["content"].strip()
        finally:
            if queued:
                async with self.queue_lock:
                    self.queued_requests -= 1


def price_catalog_text() -> str:
    return (
        "Тарифы\n\n"
        "Старт — 299 ₽/мес\n"
        "5 млн выходных токенов\n"
        "Подходит для тестов, MVP и небольших задач\n\n"
        "Pro — 999 ₽/мес\n"
        "20 млн выходных токенов\n"
        "Подходит для регулярной работы и AGI-сценариев\n\n"
        "Business — 2 990 ₽/мес\n"
        "80 млн выходных токенов\n"
        "Подходит для команды и коммерческих проектов\n\n"
        "Выбери тариф — и я сразу покажу кнопку Оплатить."
    )


def price_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Старт 299 ₽", callback_data="price:starter"),
                InlineKeyboardButton(text="Pro 999 ₽", callback_data="price:pro"),
            ],
            [
                InlineKeyboardButton(text="Business 2 990 ₽", callback_data="price:business"),
            ],
        ]
    )


def is_chat_busy(chat_id: int, busy_chats: set[int]) -> bool:
    return chat_id in busy_chats


def account_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Обзор", callback_data="lk:overview")],
            [
                InlineKeyboardButton(text="Профиль", callback_data="lk:profile"),
                InlineKeyboardButton(text="Этот чат", callback_data="lk:chat"),
            ],
            [
                InlineKeyboardButton(text="Лимит", callback_data="lk:usage"),
                InlineKeyboardButton(text="Прайс", callback_data="lk:price"),
            ],
            [
                InlineKeyboardButton(text="Сбросить историю", callback_data="lk:reset"),
            ],
        ]
    )


def account_view(
    section: str,
    user: User,
    chat: Chat,
    gigachat: GigaChatClient,
    conversations: dict[int, list[dict[str, str]]],
) -> str:
    username = f"@{user.username}" if user.username else "не задан"
    chat_type = getattr(chat.type, "value", chat.type)
    chat_title = chat.title or ("Личный чат" if chat_type == "private" else "Без названия")
    history_size = max(0, len(conversations.get(chat.id, [])) - 1)
    if section == "profile":
        return (
            "Профиль\n"
            f"Имя: {user.full_name}\n"
            f"Username: {username}\n"
            f"Telegram ID: {user.id}"
        )
    if section == "chat":
        return (
            "Текущий чат\n"
            f"Название: {chat_title}\n"
            f"Тип: {chat_type}\n"
            f"Chat ID: {chat.id}\n"
            f"Сообщений в контексте: {history_size}\n"
            "История привязана к этому чату; в группах она общая для участников."
        )
    if section == "usage":
        return (
            "Расход бота\n"
            f"Модель: {gigachat.model}\n"
            f"Использовано: {gigachat.tokens_used} токенов\n"
            "Внутренний лимит бота: выключен"
        )
    return (
        "Личный кабинет\n"
        f"Пользователь: {user.full_name} ({username})\n"
        f"Telegram ID: {user.id}\n"
        f"Текущий чат: {chat_title} ({chat_type})\n"
        f"Chat ID: {chat.id}\n"
        f"Сообщений в контексте: {history_size}\n"
        f"Модель: {gigachat.model}\n"
        f"Расход бота: {gigachat.tokens_used} токенов (внутренний лимит выключен)"
    )


def build_dispatcher(gigachat: GigaChatClient) -> Dispatcher:
    dispatcher = Dispatcher()
    router = Router()
    conversations: dict[int, list[dict[str, str]]] = {}
    chat_modes: dict[int, str] = {}
    request_times: dict[int, float] = {}
    admin_ids = parse_admin_ids(os.getenv("ADMIN_IDS", ""))
    system_prompt = os.getenv(
        "SYSTEM_PROMPT", "Ты полезный и вежливый помощник. Отвечай на русском языке."
    ).strip()

    busy_chats: set[int] = set()

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        mode = chat_modes.get(message.chat.id, "llm")
        await message.answer(
            "Привет! Я бот на GigaChat. Напиши вопрос, и я постараюсь помочь.\n"
            "Режим можно выбрать кнопками ниже: LLM или AGI-прототип.\n"
            "Открой личный кабинет кнопкой ниже или командой /account.\n"
            "Можно быстро посмотреть тестовый прайс через /price.",
            reply_markup=account_keyboard(),
        )
        await message.answer(f"Режим сейчас: {mode.upper()}", reply_markup=mode_keyboard(mode))

    @router.message(Command("mode"))
    async def mode_command(message: Message) -> None:
        mode = chat_modes.get(message.chat.id, "llm")
        await message.answer(f"Выбери режим. Сейчас: {mode.upper()}", reply_markup=mode_keyboard(mode))

    @router.callback_query(lambda query: bool(query.data and query.data.startswith("mode:")))
    async def mode_callback(callback: CallbackQuery) -> None:
        if not callback.message or not isinstance(callback.message, Message):
            await callback.answer()
            return
        mode = (callback.data or "mode:llm").partition(":")[2]
        if mode not in {"llm", "agi"}:
            await callback.answer("Неизвестный режим", show_alert=True)
            return
        chat_modes[callback.message.chat.id] = mode
        label = "AGI-прототип" if mode == "agi" else "LLM"
        await callback.message.edit_text(f"Выбран режим: {label}", reply_markup=mode_keyboard(mode))
        await callback.answer(f"Режим {label} включён")

    @router.message(Command("price"))
    async def price(message: Message) -> None:
        await message.answer(price_catalog_text(), reply_markup=price_keyboard())

    @router.message(Command("account"))
    async def account(message: Message) -> None:
        if not message.from_user or not message.chat:
            return
        await message.answer(
            account_view("overview", message.from_user, message.chat, gigachat, conversations),
            reply_markup=account_keyboard(),
        )

    @router.callback_query(lambda query: bool(query.data and query.data.startswith("lk:")))
    async def account_callback(callback: CallbackQuery) -> None:
        if not callback.message or not isinstance(callback.message, Message):
            await callback.answer()
            return

        chat = callback.message.chat
        section = (callback.data or "lk:overview").partition(":")[2]
        if section == "price":
            await callback.message.edit_text(price_catalog_text(), reply_markup=price_keyboard())
            await callback.answer("Прайс открыт")
            return
        if section == "reset":
            conversations.pop(chat.id, None)
            text = "История текущего чата очищена.\n\n" + account_view(
                "chat", callback.from_user, chat, gigachat, conversations
            )
            await callback.message.edit_text(text, reply_markup=account_keyboard())
            await callback.answer("История очищена")
            return

        if section not in {"overview", "profile", "chat", "usage"}:
            section = "overview"
        await callback.message.edit_text(
            account_view(section, callback.from_user, chat, gigachat, conversations),
            reply_markup=account_keyboard(),
        )
        await callback.answer()

    @router.callback_query(lambda query: bool(query.data and query.data.startswith("price:")))
    async def price_callback(callback: CallbackQuery) -> None:
        if not callback.message or not isinstance(callback.message, Message):
            await callback.answer()
            return
        item = (callback.data or "price:starter").partition(":")[2]
        if item == "list":
            await callback.message.edit_text(price_catalog_text(), reply_markup=price_keyboard())
            await callback.answer("Тарифы обновлены")
            return
        details = {
            "starter": (
                "Тариф Старт выбран\n\n"
                "299 ₽/мес\n"
                "5 млн выходных токенов\n\n"
                "Для тестов, MVP и небольших задач.\n"
                "Нажмите кнопку ниже, чтобы оплатить."
            ),
            "pro": (
                "Тариф Pro выбран\n\n"
                "999 ₽/мес\n"
                "20 млн выходных токенов\n\n"
                "Для регулярной работы и AGI-сценариев.\n"
                "Нажмите кнопку ниже, чтобы оплатить."
            ),
            "business": (
                "Тариф Business выбран\n\n"
                "2 990 ₽/мес\n"
                "80 млн выходных токенов\n\n"
                "Для команды и коммерческих задач.\n"
                "Нажмите кнопку ниже, чтобы оплатить."
            ),
        }.get(item, price_catalog_text())
        await callback.message.edit_text(
            details,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Назад", callback_data="price:list"),
                        InlineKeyboardButton(text="Оплатить", callback_data=f"pay:{item}"),
                    ],
                ]
            ),
        )
        await callback.answer(f"Тариф {item} выбран")

    @router.callback_query(lambda query: bool(query.data and query.data.startswith("pay:")))
    async def pay_callback(callback: CallbackQuery) -> None:
        if not callback.message or not isinstance(callback.message, Message):
            await callback.answer()
            return
        item = (callback.data or "pay:starter").partition(":")[2]
        plan_names = {
            "starter": ("Старт", "299 ₽/мес"),
            "pro": ("Pro", "999 ₽/мес"),
            "business": ("Business", "2 990 ₽/мес"),
        }
        name, price = plan_names.get(item, ("Старт", "299 ₽/мес"))
        await callback.message.edit_text(
            f"Оплатить: {name} — {price}\n\n"
            "Выберите способ оплаты в следующем шаге.\n"
            "Это тестовый сценарий оплаты для тарифа.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Назад", callback_data=f"price:{item}"),
                        InlineKeyboardButton(text="Подтвердить оплату", callback_data=f"checkout:{item}"),
                    ],
                ]
            ),
        )
        await callback.answer("Оплата подготовлена")

    @router.callback_query(lambda query: bool(query.data and query.data.startswith("price:list")))
    async def price_list_callback(callback: CallbackQuery) -> None:
        if not callback.message or not isinstance(callback.message, Message):
            await callback.answer()
            return
        await callback.message.edit_text(price_catalog_text(), reply_markup=price_keyboard())
        await callback.answer("Тарифы обновлены")

    @router.message(Command("reset"))
    async def reset(message: Message) -> None:
        if message.chat:
            conversations.pop(message.chat.id, None)
        await message.answer("История диалога очищена.")

    @router.message(Command("admin"))
    async def admin(message: Message) -> None:
        if not message.from_user:
            return
        if not allow_request(message.from_user.id, request_times, time.monotonic()):
            return
        if message.from_user.id not in admin_ids:
            await message.answer("Доступ запрещён.")
            return
        await message.answer(
            f"Панель тестирования\nМодель: {gigachat.model}\n"
            f"Использовано: {gigachat.tokens_used} токенов\n"
            "Внутренний лимит бота: выключен"
        )

    @router.message(Command("agent"))
    async def agent_command(message: Message) -> None:
        if not message.chat:
            return
        await message.answer(agent_status(message.chat.id, "prototype"))

    @router.message(Command("tools"))
    async def tools_command(message: Message) -> None:
        lines = ["Доступные AGI-инструменты:"]
        for tool_name, description in AGENT_TOOLS.items():
            lines.append(f"- {tool_name}: {description}")
        await message.answer("\n".join(lines))

    @router.message(Command("plan"))
    async def plan_command(message: Message) -> None:
        goal = (message.get_args() or "Нужно подготовить рабочую архитектуру AGI").strip()
        plan = build_agent_plan(goal)
        steps = "\n".join(f"{index + 1}. {step}" for index, step in enumerate(plan["steps"]))
        await message.answer(f"Цель: {plan['goal']}\n\nПлан:\n{steps}\n\nИтог: {plan['summary']}")

    @router.message()
    async def chat(message: Message) -> None:
        text = (message.text or "").strip()
        if not text:
            return

        if message.from_user and not allow_request(message.from_user.id, request_times, time.monotonic()):
            await message.answer("Слишком часто. Подожди пару секунд перед следующим запросом.")
            return

        if message.chat and is_chat_busy(message.chat.id, busy_chats):
            await message.answer("⏳ Запрос уже выполняется в этом чате. Подождите, пока текущий запрос завершится.")
            return

        if text.lower().startswith("!plan "):
            goal = text[6:].strip() or "Нужно подготовить рабочую архитектуру AGI"
            plan = build_agent_plan(goal)
            steps = "\n".join(f"{index + 1}. {step}" for index, step in enumerate(plan["steps"]))
            await message.answer(f"Цель: {plan['goal']}\n\nПлан:\n{steps}\n\nИтог: {plan['summary']}")
            return

        if text.lower().startswith("!tool "):
            command = text[6:].strip()
            if " " in command:
                tool_name, raw_payload = command.split(" ", 1)
                payload = {"goal": raw_payload.strip()}
            else:
                tool_name = command
                payload = {}
            await message.answer(execute_agent_tool(tool_name, payload))
            return

        if text.lower().startswith("агент ") or text.lower().startswith("!agent "):
            goal = text.split(" ", 1)[1].strip() if " " in text else "Нужно подготовить рабочую архитектуру AGI"
            plan = build_agent_plan(goal)
            await message.answer(
                "AGI-режим активен\n"
                f"Цель: {plan['goal']}\n"
                f"{plan['summary']}"
            )
            return

        if not message.text or not message.chat:
            await message.answer("Пока я умею отвечать только на текстовые сообщения.")
            return

        active_mode = chat_modes.get(message.chat.id, "llm")
        history = conversations.setdefault(
            message.chat.id,
            [{"role": "system", "content": mode_prompt(active_mode, system_prompt)}],
        )
        history[0]["content"] = mode_prompt(active_mode, system_prompt)
        history.append({"role": "user", "content": message.text})
        busy_chats.add(message.chat.id)
        progress = await message.answer("⏳ Запрос принят. Ставлю в работу...")
        typing_task: asyncio.Task | None = None

        async def update_queue(position: int) -> None:
            await progress.edit_text(
                f"⏳ Запрос в очереди. Позиция при постановке: {position}."
            )

        async def show_thinking() -> None:
            nonlocal typing_task
            frames = (
                "🧠 Думаю\n[●○○○]",
                "🧠 Думаю\n[○●○○]",
                "🧠 Думаю\n[○○●○]",
                "🧠 Думаю\n[○○○●]",
            )
            await progress.edit_text(frames[0])

            async def animate_thinking() -> None:
                frame_index = 1
                while True:
                    await asyncio.sleep(1.2)
                    try:
                        await progress.edit_text(frames[frame_index % len(frames)])
                    except TelegramAPIError:
                        logger.debug("Could not animate thinking message", exc_info=True)
                    frame_index += 1
                    if frame_index % 3 == 0:
                        try:
                            await message.bot.send_chat_action(
                                chat_id=message.chat.id,
                                action=ChatAction.TYPING,
                            )
                        except TelegramAPIError:
                            logger.debug("Could not send typing action", exc_info=True)

            typing_task = asyncio.create_task(animate_thinking())

        try:
            answer = await gigachat.complete(
                history,
                on_queued=update_queue,
                on_started=show_thinking,
            )
        except (httpx.HTTPError, KeyError, ValueError):
            history.pop()
            logger.exception("GigaChat request failed")
            await progress.edit_text("Не получилось получить ответ от GigaChat. Попробуй чуть позже.")
            return
        finally:
            if typing_task:
                typing_task.cancel()
                await asyncio.gather(typing_task, return_exceptions=True)
            busy_chats.discard(message.chat.id)

        history.append({"role": "assistant", "content": answer})
        if len(history) > 21:
            del history[1 : len(history) - 19]
        chunks = [answer[offset : offset + 4000] for offset in range(0, len(answer), 4000)]
        await progress.edit_text(chunks[0])
        for chunk in chunks[1:]:
            await message.answer(chunk)

    dispatcher.include_router(router)
    return dispatcher


async def run() -> None:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError("Заполните BOT_TOKEN в файле .env")

    gigachat = GigaChatClient()
    try:
        async with Bot(bot_token) as bot:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Открыть бота"),
                    BotCommand(command="account", description="Личный кабинет"),
                    BotCommand(command="price", description="Тарифы и цены"),
                    BotCommand(command="admin", description="Сводка тестирования"),
                    BotCommand(command="reset", description="Сбросить историю чата"),
                    BotCommand(command="mode", description="Выбрать режим LLM или AGI"),
                ]
            )
            try:
                await bot.set_my_name("Vega AGI")
                await bot.set_my_description(
                    "Vega AGI: помощник на GigaChat с режимами LLM и AGI-прототипа."
                )
            except TelegramAPIError:
                logger.warning("Не удалось обновить имя/описание профиля бота", exc_info=True)
            await build_dispatcher(gigachat).start_polling(bot)
    finally:
        await gigachat.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run())
    except (ValueError, httpx.HTTPError) as error:
        logger.error("Не удалось запустить бота: %s", error)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()