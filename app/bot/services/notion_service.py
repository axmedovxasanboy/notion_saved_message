from os import getenv

from aiogram import Bot
from aiogram.types import Message, CallbackQuery
import os

from bot import keyboards
from bot.model.bot_models import BotSteps
from bot.services import user_service
from exceptions.bot_exceptions import ArgumentsNotConfiguredCorrectlyException
from notion import notion_service
from exceptions.notion_exceptions import NotionPageIdNotSpecified
from bot.model import bot_models

ADMIN_CHAT_ID = os.getenv("CHAT_ID", "NONE")


async def handle_main_workspace(message: Message, bot: Bot):
    try:
        chat_id = str(message.chat.id)
        if ADMIN_CHAT_ID != chat_id:
            await bot.send_message(chat_id, "What notion space are you talking about?")
            return

        user = user_service.get_user_by_chat_id(chat_id)

        if user is None:
            user = user_service.save_or_update_user(user_msg=message)

        page = await notion_service.get_page_contents()
        header = f"🖥 You're now in the Notion workspace — <b>{page.title}</b>\n\nTap a page to open it:"
        callback_queries = keyboards.get_page_callback_queries(page.page)
        notion_workspace_buttons = keyboards.get_notion_workspace_page_buttons()
        user.step = bot_models.BotSteps.WORKSPACE

        await bot.send_message(chat_id, header, reply_markup=callback_queries)
        await bot.send_message(chat_id, "Or use the menu below 👇", reply_markup=notion_workspace_buttons)
        user_service.save_or_update_user(user=user)

    except NotionPageIdNotSpecified as notion_page_id_not_specified:
        await bot.send_message(ADMIN_CHAT_ID, notion_page_id_not_specified.format_error())
    except ArgumentsNotConfiguredCorrectlyException as args_error:
        await bot.send_message(ADMIN_CHAT_ID, args_error.format_error())


async def page_callback_request(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None:
        return
    chat_id = str(query.message.chat.id)
    query_data = query.data
    page_id = query_data.replace("_NOTION_PAGE_ID_", "")
    page = await notion_service.get_page_contents(page_id)
    admin_msg = (f"Main page: 📃<b>{page.title}</b>\n"
                 f"It contains {len(page.paragraphs)} paragraphs and {len(page.page)} pages\n"
                 f"Below is the structured format of notion page\n\n")

    full_text = notion_service.get_notion_page_content_fully(page)
    callback_queries = keyboards.get_page_callback_queries(page.page, True)

    if len(page.page) >= 15:
        full_text += "\n<i>Note: This may not be full list of actual notion page. For detailed information click buttons below.</i>"

    await bot.edit_message_text(text=admin_msg + full_text, chat_id=chat_id, message_id=query.message.message_id, reply_markup=callback_queries)
    # await bot.send_message(os.getenv("CHAT_ID", "NONE"), admin_msg + full_text, reply_markup=callback_queries, reply_to_message_id=query.message.message_id)

    user = user_service.get_user_by_chat_id(chat_id)

    if query_data:
        if (getenv("NOTION_CHANNEL_POSTS_PAGE_ID", "34f4395b-313a-803b-9523-c89f176caff0")) == page_id:
            user.callback_step = BotSteps.CALLBACK_CHANNELS
        elif getenv("NOTION_POEMS_PAGE_ID", "34f4395b-313a-8009-a882-ca5f07d2d144") == page_id:
            user.callback_step = BotSteps.CALLBACK_POEMS
        elif getenv("NOTION_USER_QUOTES_PAGE_ID", "34f4395b-313a-80a9-89b6-e70c571420e6") == page_id:
            user.callback_step = BotSteps.CALLBACK_USER_QUOTES

    user_service.save_or_update_user(user=user)


async def page_back_to_main(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None:
        return

    chat_id = str(query.message.chat.id)
    user = user_service.get_user_by_chat_id(chat_id)
    if user is None:
        return
    cbk_step = user.callback_step

    if cbk_step.value != BotSteps.CALLBACK_MAIN:
        user.callback_step = BotSteps.CALLBACK_MAIN

    user_service.save_or_update_user(user=user)

    page = await notion_service.get_page_contents()
    header = f"🖥 You're now in the Notion workspace — <b>{page.title}</b>\n\nTap a page to open it:"
    callback_queries = keyboards.get_page_callback_queries(page.page)

    await bot.edit_message_text(text=header, message_id=query.message.message_id, chat_id=chat_id, reply_markup=callback_queries)

async def back(message: Message, bot: Bot) -> None:
    chat_id = str(message.chat.id)
    try:
        if ADMIN_CHAT_ID != chat_id:
            await bot.send_message(chat_id, "???")
            return

        user = user_service.get_user_by_chat_id(chat_id)
        admin_keyboards = keyboards.get_admin_keyboards()

        if user is None or user.step == BotSteps.MAIN:
            await bot.send_message(
                chat_id, "You are on the main page. No need to back", reply_markup=admin_keyboards,
            )
            return

        user.step = BotSteps.MAIN
        user_service.save_or_update_user(user=user)
        await bot.send_message(chat_id, "Back to main page", reply_markup=admin_keyboards)

    except Exception as e:
        await bot.send_message(chat_id, str(e))




