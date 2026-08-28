import os
from os import getenv

from aiogram import Bot
from aiogram.types import Message, CallbackQuery

from bot import keyboards
from bot.model.bot_models import BotSteps
from bot.services import user_service
from bot.text_utils import esc
from exceptions.bot_exceptions import ArgumentsNotConfiguredCorrectlyException
from notion import notion_service
from exceptions.notion_exceptions import NotionPageIdNotSpecified
from bot.model import bot_models

ADMIN_CHAT_ID = os.getenv("CHAT_ID")


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
        header = f"🖥 You're now in the Notion workspace — <b>{esc(page.title)}</b>\n\nTap a page to open it:"
        callback_queries = keyboards.get_page_callback_queries(page.page)
        notion_workspace_buttons = keyboards.get_notion_workspace_page_buttons()
        user.step = bot_models.BotSteps.WORKSPACE

        await bot.send_message(chat_id, header, reply_markup=callback_queries)
        await bot.send_message(chat_id, "Or use the menu below 👇", reply_markup=notion_workspace_buttons)
        user_service.save_or_update_user(user=user)

    except NotionPageIdNotSpecified as notion_page_id_not_specified:
        if ADMIN_CHAT_ID:
            await bot.send_message(ADMIN_CHAT_ID, esc(notion_page_id_not_specified.format_error()))
    except ArgumentsNotConfiguredCorrectlyException as args_error:
        if ADMIN_CHAT_ID:
            await bot.send_message(ADMIN_CHAT_ID, esc(args_error.format_error()))


async def page_callback_request(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None or not query.data:
        return
    chat_id = str(query.message.chat.id)
    page_id = query.data.replace("_NOTION_PAGE_ID_", "")
    try:
        page = await notion_service.get_page_contents(page_id)
    except Exception as exc:  # noqa: BLE001 — a Notion hiccup must not leave the spinner hanging
        await _answer(bot, query, text=f"Couldn't open the page: {exc}"[:200], alert=True)
        return
    admin_msg = (f"Main page: 📃<b>{esc(page.title)}</b>\n"
                 f"It contains {len(page.paragraphs)} paragraphs and {len(page.page)} pages\n"
                 f"Below is the structured format of notion page\n\n")

    full_text = notion_service.get_notion_page_content_fully(page)
    callback_queries = keyboards.get_page_callback_queries(page.page, True)

    if len(page.page) >= 15:
        full_text += "\n<i>Note: This may not be full list of actual notion page. For detailed information click buttons below.</i>"

    await _edit_ignore_not_modified(
        bot, text=admin_msg + full_text, chat_id=chat_id,
        message_id=query.message.message_id, reply_markup=callback_queries,
    )

    user = user_service.get_user_by_chat_id(chat_id)
    if user is not None:
        # No hardcoded fallbacks here: page ids are workspace-internal values that
        # belong in .env only. An unset env var simply never matches.
        if getenv("NOTION_CHANNEL_POSTS_PAGE_ID") == page_id:
            user.callback_step = BotSteps.CALLBACK_CHANNELS
        elif getenv("NOTION_POEMS_PAGE_ID") == page_id:
            user.callback_step = BotSteps.CALLBACK_POEMS
        elif getenv("NOTION_USER_QUOTES_PAGE_ID") == page_id:
            user.callback_step = BotSteps.CALLBACK_USER_QUOTES
        user_service.save_or_update_user(user=user)

    await _answer(bot, query)


async def page_back_to_main(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None:
        return

    chat_id = str(query.message.chat.id)
    user = user_service.get_user_by_chat_id(chat_id)
    if user is None:
        await _answer(bot, query)
        return

    if user.callback_step != BotSteps.CALLBACK_MAIN:
        user.callback_step = BotSteps.CALLBACK_MAIN
        user_service.save_or_update_user(user=user)

    try:
        page = await notion_service.get_page_contents()
    except Exception as exc:  # noqa: BLE001
        await _answer(bot, query, text=f"Couldn't load the workspace: {exc}"[:200], alert=True)
        return
    header = f"🖥 You're now in the Notion workspace — <b>{esc(page.title)}</b>\n\nTap a page to open it:"
    callback_queries = keyboards.get_page_callback_queries(page.page)

    await _edit_ignore_not_modified(
        bot, text=header, message_id=query.message.message_id,
        chat_id=chat_id, reply_markup=callback_queries,
    )
    await _answer(bot, query)


async def _edit_ignore_not_modified(bot: Bot, **kwargs) -> None:
    """edit_message_text that treats Telegram's 'message is not modified'
    (a re-tap re-rendering identical content) as success."""
    try:
        await bot.edit_message_text(**kwargs)
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def _answer(bot: Bot, query: CallbackQuery, text: str = None, alert: bool = False) -> None:
    """Ack a callback; never let the ack itself take the handler down."""
    try:
        await bot.answer_callback_query(query.id, text=text, show_alert=alert)
    except Exception:  # noqa: BLE001
        pass


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

    except Exception as e:  # noqa: BLE001 — surface the failure instead of dropping it
        await bot.send_message(chat_id, esc(e))
