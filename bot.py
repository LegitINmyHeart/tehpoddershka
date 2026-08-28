import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from typing import Any, Awaitable, Callable, Dict

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, TelegramObject
from aiogram.enums import ParseMode

# ============ НАСТРОЙКИ ============
# Токен и ID группы теперь берутся из переменных окружения (Railway → Variables),
# а не хранятся в коде — это важно для безопасности.
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_GROUP_ID = int(os.environ["ADMIN_GROUP_ID"])
DB_PATH = "tickets.db"
# ====================================

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)


# ---------- DEBUG MIDDLEWARE: логирует chat_id любого сообщения ----------
# Это нужно, чтобы узнать правильный ADMIN_GROUP_ID для группы админов.
# После того как ID найден и прописан в переменных окружения, этот блок
# можно оставить как есть — он ничего не ломает и просто пишет в логи.
class ChatIdLoggerMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            chat = event.chat
            logging.info(
                f"[CHAT DEBUG] chat_id={chat.id} type={chat.type} title={chat.title!r}"
            )
        return await handler(event, data)


dp.update.middleware(ChatIdLoggerMiddleware())


# ---------- БАЗА ДАННЫХ ----------
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                admin_msg_id INTEGER,
                status TEXT DEFAULT 'open',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def create_ticket(user_id: int, username: str) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "INSERT INTO tickets (user_id, username) VALUES (?, ?)",
            (user_id, username or "без username"),
        )
        conn.commit()
        return cur.lastrowid


def set_admin_msg_id(ticket_id: int, admin_msg_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE tickets SET admin_msg_id = ? WHERE id = ?",
            (admin_msg_id, ticket_id),
        )
        conn.commit()


def get_ticket_by_admin_msg(admin_msg_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT id, user_id, status FROM tickets WHERE admin_msg_id = ?",
            (admin_msg_id,),
        )
        return cur.fetchone()


def close_ticket(ticket_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE tickets SET status = 'closed' WHERE id = ?", (ticket_id,))
        conn.commit()


def get_ticket(ticket_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT id, user_id, status, admin_msg_id FROM tickets WHERE id = ?",
            (ticket_id,),
        )
        return cur.fetchone()


# ---------- КЛАВИАТУРЫ ----------
def admin_keyboard(ticket_id: int, status: str = "open") -> InlineKeyboardMarkup:
    if status == "open":
        buttons = [[
            InlineKeyboardButton(text="✅ Взять в работу", callback_data=f"take:{ticket_id}"),
            InlineKeyboardButton(text="❌ Закрыть", callback_data=f"close:{ticket_id}"),
        ]]
    elif status == "in_progress":
        buttons = [[
            InlineKeyboardButton(text="❌ Закрыть", callback_data=f"close:{ticket_id}"),
        ]]
    else:
        buttons = []
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------- ХЕНДЛЕРЫ ДЛЯ ИГРОКОВ (ЛС) ----------
@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Это бот тех.поддержки сервера.\n\n"
        "Просто опиши свою проблему одним сообщением — "
        "и оно попадёт к администраторам. Как только тебе ответят, "
        "ты получишь уведомление прямо здесь."
    )


@router.message(Command("status"), F.chat.type == "private")
async def cmd_status(message: Message):
    await message.answer("Проверка статуса сервера пока не настроена. См. README.md")


@router.message(F.chat.type == "private", F.text)
async def new_ticket(message: Message):
    if message.text.startswith("/"):
        return

    ticket_id = create_ticket(message.from_user.id, message.from_user.username)

    admin_text = (
        f"🎫 <b>Тикет #{ticket_id}</b>\n"
        f"От: {message.from_user.full_name} (@{message.from_user.username or 'нет'})\n"
        f"ID: <code>{message.from_user.id}</code>\n\n"
        f"💬 {message.text}"
    )

    sent = await bot.send_message(
        ADMIN_GROUP_ID,
        admin_text,
        reply_markup=admin_keyboard(ticket_id, "open"),
    )
    set_admin_msg_id(ticket_id, sent.message_id)

    await message.answer(f"✅ Обращение принято, номер тикета #{ticket_id}. Ожидай ответа админа.")


# ---------- ХЕНДЛЕРЫ ДЛЯ АДМИНОВ (в группе) ----------
@router.callback_query(F.data.startswith("take:"))
async def take_ticket(callback: CallbackQuery):
    ticket_id = int(callback.data.split(":")[1])
    row = get_ticket(ticket_id)
    if not row:
        await callback.answer("Тикет не найден", show_alert=True)
        return

    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE tickets SET status = 'in_progress' WHERE id = ?", (ticket_id,))
        conn.commit()

    await callback.message.edit_reply_markup(reply_markup=admin_keyboard(ticket_id, "in_progress"))
    await callback.answer(f"Взял тикет #{ticket_id} в работу ({callback.from_user.full_name})")


@router.callback_query(F.data.startswith("close:"))
async def close_ticket_cb(callback: CallbackQuery):
    ticket_id = int(callback.data.split(":")[1])
    row = get_ticket(ticket_id)
    if not row:
        await callback.answer("Тикет не найден", show_alert=True)
        return

    _, user_id, _, _ = row
    close_ticket(ticket_id)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"🔒 Тикет #{ticket_id} закрыт админом {callback.from_user.full_name}")

    try:
        await bot.send_message(user_id, f"🔒 Твой тикет #{ticket_id} закрыт. Если вопрос не решён — напиши новое сообщение.")
    except Exception as e:
        logging.warning(f"Не удалось уведомить пользователя {user_id}: {e}")

    await callback.answer("Тикет закрыт")


@router.message(F.chat.id == ADMIN_GROUP_ID, F.reply_to_message)
async def admin_reply(message: Message):
    """Админ отвечает на пересланное сообщение тикета (Reply в группе) — ответ уходит игроку."""
    replied = message.reply_to_message
    row = get_ticket_by_admin_msg(replied.message_id)
    if not row:
        return

    ticket_id, user_id, status = row
    if status == "closed":
        await message.reply("⚠️ Этот тикет уже закрыт.")
        return

    try:
        await bot.send_message(
            user_id,
            f"💬 <b>Ответ администратора по тикету #{ticket_id}:</b>\n\n{message.text}",
        )
        await message.reply("✅ Отправлено игроку")
    except Exception as e:
        await message.reply(f"❌ Не удалось отправить: {e}")


async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
