import asyncio
import logging
from os import getenv

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message
from dotenv import load_dotenv

from notion import notion_service as notion_api

from .handlers import callback_query, channels, favorites, forward, settings, start
from .services import notion_service, sync_service

_log = logging.getLogger(__name__)

load_dotenv()
dp = Dispatcher()
handler_bot = Bot(token=getenv("BOT_TOKEN", ""), default=DefaultBotProperties(parse_mode=ParseMode.HTML))


@dp.message(CommandStart())
async def start_command(message: Message) -> None:
    await start.welcome_message(message, handler_bot)


@dp.message(F.text == getenv("NOTION_MAIN_WORKSPACE_BUTTON_TEXT"))
async def notion(message: Message) -> None:
    await notion_service.handle_main_workspace(message, handler_bot)


@dp.message(F.text == getenv("NOTION_BACK_BUTTON_TEXT"))
async def back(message: Message) -> None:
    await notion_service.back(message, handler_bot)


@dp.message(F.text == "Channels 📺")
async def channels_button(message: Message) -> None:
    await channels.open_channels(message, handler_bot)


@dp.message(F.text == "Sync 🔄")
async def sync_button(message: Message) -> None:
    await channels.open_sync(message, handler_bot)


@dp.message(F.text == "Settings ⚙️")
async def settings_button(message: Message) -> None:
    await settings.open_settings(message, handler_bot)


@dp.message(F.text == "Favorites ⭐")
async def favorites_button(message: Message) -> None:
    await favorites.open_favorites(message, handler_bot)


@dp.edited_message()
async def update(message: Message) -> None:
    if str(message.chat.id) != getenv("CHAT_ID"):
        return
    await handler_bot.send_message(message.chat.id, "Please send message instead of editing it")


@dp.message(F.forward_origin)
async def forward_message(message: Message) -> None:
    await forward.forward_message(message, handler_bot)


@dp.message(F.text)
async def text_message(message: Message) -> None:
    if await callback_query.receive_custom_title(message, handler_bot):
        return
    if await channels.handle_text_input(message, handler_bot):
        return
    await start.welcome_message(message, handler_bot)


@dp.callback_query(F.data.contains("_NOTION_PAGE_ID_"))
async def notion_page_request(query: CallbackQuery) -> None:
    await notion_service.page_callback_request(query, handler_bot)


@dp.callback_query(F.data.startswith("REGENERATE_BY_GPT_"))
async def regenerate_by_gpt(query: CallbackQuery) -> None:
    await callback_query.regenerate(query, handler_bot, by_gpt=True)


@dp.callback_query(F.data.startswith("REGENERATE_BY_CLAUDE_"))
async def regenerate_by_claude(query: CallbackQuery) -> None:
    await callback_query.regenerate(query, handler_bot, by_claude=True)


@dp.callback_query(F.data.startswith("ASK_FROM_CLAUDE_"))
async def ask_from_claude(query: CallbackQuery) -> None:
    await callback_query.ask_from_ai(query, handler_bot, from_claude=True)


@dp.callback_query(F.data.startswith("ASK_FROM_GPT_"))
async def ask_from_gpt(query: CallbackQuery) -> None:
    await callback_query.ask_from_ai(query, handler_bot, from_gpt=True)


@dp.callback_query(F.data.startswith("TITLE_BY_ME_"))
async def title_by_me(query: CallbackQuery) -> None:
    await callback_query.request_custom_title(query, handler_bot)


@dp.callback_query(F.data.startswith("SAVE_WITH_GPT_TITLE_"))
async def save_with_gpt_title(query: CallbackQuery) -> None:
    await callback_query.save_to_notion(query, handler_bot, source="gpt")


@dp.callback_query(F.data.startswith("SAVE_WITH_CLAUDE_TITLE_"))
async def save_with_claude_title(query: CallbackQuery) -> None:
    await callback_query.save_to_notion(query, handler_bot, source="claude")


@dp.callback_query(F.data.startswith("SAVE_WITH_MY_TITLE_"))
async def save_with_my_title(query: CallbackQuery) -> None:
    await callback_query.save_to_notion(query, handler_bot, source="custom")


@dp.callback_query(F.data.contains("BACK_TO_MAIN"))
async def notion_back_to_previous(query: CallbackQuery) -> None:
    await notion_service.page_back_to_main(query, handler_bot)


# ----- Channels management callbacks -----

@dp.callback_query(F.data.startswith("CH_LIST"))
async def channels_list_cbq(query: CallbackQuery) -> None:
    # Matches CH_LIST (open page 0), CH_LIST_PAGE_<n> (pagination), and
    # CH_LIST_NOOP (the page-indicator pill — handler just acks it).
    await channels.show_channel_list(query, handler_bot)


@dp.callback_query(F.data.startswith("CH_VIEW_"))
async def channel_view_cbq(query: CallbackQuery) -> None:
    await channels.show_channel(query, handler_bot)


@dp.callback_query(F.data.startswith("CH_POSTS_"))
async def channel_posts_cbq(query: CallbackQuery) -> None:
    await channels.show_channel_posts(query, handler_bot)


@dp.callback_query(F.data.startswith("CH_RENAME_"))
async def channel_rename_cbq(query: CallbackQuery) -> None:
    await channels.request_rename_channel(query, handler_bot)


@dp.callback_query(F.data.startswith("CH_USERNAME_"))
async def channel_username_cbq(query: CallbackQuery) -> None:
    await channels.request_set_username(query, handler_bot)


@dp.callback_query(F.data.startswith("CH_MERGE_GO_"))
async def channel_merge_go_cbq(query: CallbackQuery) -> None:
    await channels.execute_merge(query, handler_bot)


@dp.callback_query(F.data.startswith("CH_MERGE_"))
async def channel_merge_cbq(query: CallbackQuery) -> None:
    await channels.request_merge(query, handler_bot)


@dp.callback_query(F.data.startswith("CH_DELETE_GO_"))
async def channel_delete_go_cbq(query: CallbackQuery) -> None:
    await channels.execute_delete_channel(query, handler_bot)


@dp.callback_query(F.data.startswith("CH_DELETE_"))
async def channel_delete_cbq(query: CallbackQuery) -> None:
    await channels.request_delete_channel(query, handler_bot)


@dp.callback_query(F.data.startswith("POST_VIEW_"))
async def post_view_cbq(query: CallbackQuery) -> None:
    await channels.show_post(query, handler_bot)


@dp.callback_query(F.data.startswith("POST_TITLE_"))
async def post_title_cbq(query: CallbackQuery) -> None:
    await channels.request_post_title(query, handler_bot)


@dp.callback_query(F.data.startswith("POST_MOVE_GO_"))
async def post_move_go_cbq(query: CallbackQuery) -> None:
    await channels.execute_post_move(query, handler_bot)


@dp.callback_query(F.data.startswith("POST_MOVE_"))
async def post_move_cbq(query: CallbackQuery) -> None:
    await channels.request_post_move(query, handler_bot)


@dp.callback_query(F.data.startswith("POST_DELETE_GO_"))
async def post_delete_go_cbq(query: CallbackQuery) -> None:
    await channels.execute_delete_post(query, handler_bot)


@dp.callback_query(F.data.startswith("POST_DELETE_"))
async def post_delete_cbq(query: CallbackQuery) -> None:
    await channels.request_delete_post(query, handler_bot)


# Order matters: the more specific POST_MERGE_GO_, POST_MERGE_PICK_,
# POST_MERGE_PAGE_, POST_MERGE_NOOP variants must register BEFORE the bare
# POST_MERGE_ prefix or the latter would swallow them.
@dp.callback_query(F.data.startswith("POST_MERGE_GO_"))
async def post_merge_go_cbq(query: CallbackQuery) -> None:
    await channels.execute_post_merge(query, handler_bot)


@dp.callback_query(F.data.startswith("POST_MERGE_PICK_"))
async def post_merge_pick_cbq(query: CallbackQuery) -> None:
    await channels.request_merge_date(query, handler_bot)


@dp.callback_query(F.data.startswith("POST_MERGE_PAGE_"))
async def post_merge_page_cbq(query: CallbackQuery) -> None:
    await channels.request_post_merge(query, handler_bot)


@dp.callback_query(F.data == "POST_MERGE_NOOP")
async def post_merge_noop_cbq(query: CallbackQuery) -> None:
    await channels.request_post_merge(query, handler_bot)


@dp.callback_query(F.data.startswith("POST_MERGE_"))
async def post_merge_cbq(query: CallbackQuery) -> None:
    await channels.request_post_merge(query, handler_bot)


@dp.callback_query(F.data.startswith("SET_"))
async def settings_cbq(query: CallbackQuery) -> None:
    await settings.handle_callback(query, handler_bot)


# ----- Favorites callbacks -----

@dp.callback_query(F.data == "FAV_MENU")
async def favorites_menu_cbq(query: CallbackQuery) -> None:
    await favorites.show_menu(query, handler_bot)


@dp.callback_query(F.data == "FAV_TYPE_CH")
async def favorites_type_channels_cbq(query: CallbackQuery) -> None:
    await favorites.show_favorite_channels(query, handler_bot)


@dp.callback_query(F.data == "FAV_TYPE_POST")
async def favorites_type_posts_cbq(query: CallbackQuery) -> None:
    await favorites.show_favorite_posts(query, handler_bot)


@dp.callback_query(F.data.startswith("FAV_OPEN_CH_"))
async def favorites_open_channel_cbq(query: CallbackQuery) -> None:
    await favorites.open_favorite_channel(query, handler_bot)


@dp.callback_query(F.data.startswith("FAV_OPEN_POST_"))
async def favorites_open_post_cbq(query: CallbackQuery) -> None:
    await favorites.open_favorite_post(query, handler_bot)


@dp.callback_query(F.data.startswith("FAV_TOGGLE_CH_"))
async def favorites_toggle_channel_cbq(query: CallbackQuery) -> None:
    await favorites.toggle_channel_favorite(query, handler_bot)


@dp.callback_query(F.data.startswith("FAV_TOGGLE_POST_"))
async def favorites_toggle_post_cbq(query: CallbackQuery) -> None:
    await favorites.toggle_post_favorite(query, handler_bot)


async def main() -> None:
    # Idempotently create (or find) the three root databases inside the
    # Channels / Poems / User Quotes pages before any forward is processed.
    # If this fails, every save flow downstream is broken — better to abort
    # startup with a clear log line than poll-loop while save_post returns 400s.
    await notion_api.bootstrap_root_databases()
    asyncio.create_task(sync_service.auto_sync_loop())
    await dp.start_polling(handler_bot)
