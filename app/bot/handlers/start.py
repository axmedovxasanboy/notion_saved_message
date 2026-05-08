from os import getenv

from aiogram import Bot
from aiogram.types import Message

from .. import keyboards
from ..model.bot_models import BotSteps
from bot.services import user_service

ADMIN_CHAT_ID = getenv("CHAT_ID")


async def welcome_message(message: Message, bot: Bot):
    msg_welcome = getenv("MSG_WELCOME")
    msg_welcome_admin = getenv("MSG_WELCOME_ADMIN")
    chat_id = str(message.chat.id)

    if chat_id != ADMIN_CHAT_ID:
        # Per project rule: never persist anything from non-admin users.
        await bot.send_message(chat_id, msg_welcome)
        return

    user = user_service.get_user_by_chat_id(chat_id)
    if user is None:
        user_service.save_or_update_user(user_msg=message)
    else:
        user.step = BotSteps.MAIN
        user_service.save_or_update_user(user=user)

    keys = keyboards.get_admin_keyboards()
    await bot.send_message(ADMIN_CHAT_ID, msg_welcome_admin, reply_markup=keys)
