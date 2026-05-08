import os
from typing import Optional

from aiogram import Bot
from aiogram.types import CallbackQuery, Message

from bot import keyboards
from bot.model.bot_models import BotSteps, User
from bot.services import user_service

ADMIN_CHAT_ID = os.getenv("CHAT_ID")


async def open_settings(message: Message, bot: Bot) -> None:
    chat_id = str(message.chat.id)
    if chat_id != ADMIN_CHAT_ID:
        await bot.send_message(chat_id, "Settings are admin-only.")
        return
    user = user_service.get_user_by_chat_id(chat_id)
    if user is None:
        return
    user.step = BotSteps.SETTINGS
    user_service.save_or_update_user(user=user)
    await bot.send_message(chat_id, _format(user), reply_markup=keyboards.get_settings_keyboard(user))


async def handle_callback(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None or not query.data:
        return
    chat_id = str(query.message.chat.id)
    user = user_service.get_user_by_chat_id(chat_id)
    if user is None:
        await bot.answer_callback_query(query.id)
        return

    data = query.data
    if data.startswith("SET_AI_"):
        ai = data.replace("SET_AI_", "", 1)
        try:
            user = user_service.set_auto_title_ai(user, ai)
        except ValueError as exc:
            await bot.answer_callback_query(query.id, text=str(exc), show_alert=True)
            return
    elif data == "SET_AUTOSAVE_TOGGLE":
        user = user_service.set_auto_save(user, not user.auto_save)
    elif data.startswith("SET_SYNC_"):
        minutes = _parse_int(data, "SET_SYNC_")
        if minutes is None:
            await bot.answer_callback_query(query.id)
            return
        user = user_service.set_auto_sync_interval(user, minutes)
    else:
        await bot.answer_callback_query(query.id)
        return

    await bot.edit_message_text(
        text=_format(user),
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
        reply_markup=keyboards.get_settings_keyboard(user),
    )
    await bot.answer_callback_query(query.id, text="Saved.")


def _format(user: User) -> str:
    ai_label = "GPT" if user.auto_title_ai == "gpt" else "Claude"
    save_label = "ON" if user.auto_save else "OFF"
    sync = user.auto_sync_interval_minutes
    if sync <= 0:
        sync_label = "Off"
    elif sync < 60:
        sync_label = f"every {sync} minutes"
    elif sync % 60 == 0:
        hours = sync // 60
        sync_label = f"every {hours} hour{'s' if hours != 1 else ''}"
    else:
        sync_label = f"every {sync} minutes"
    return (
        "<b>⚙️ Settings</b>\n\n"
        f"🤖 Auto-title AI: <b>{ai_label}</b>\n"
        f"💾 Auto-save forwards: <b>{save_label}</b>\n"
        f"⏰ Auto-sync from Notion: <b>{sync_label}</b>"
    )


def _parse_int(data: str, prefix: str) -> Optional[int]:
    raw = data.replace(prefix, "", 1)
    try:
        return int(raw)
    except ValueError:
        return None
