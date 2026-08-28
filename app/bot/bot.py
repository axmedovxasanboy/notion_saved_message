import asyncio
import logging
import sys
from os import getenv

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from dotenv import load_dotenv

from notion import notion_service as notion_api

from .handlers import callback_query, channels, favorites, forward, settings, start
from .services import notion_service, sync_service

_log = logging.getLogger(__name__)

load_dotenv()

_BOT_TOKEN = getenv("BOT_TOKEN", "").strip()
if not _BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set — add it to .env before starting the bot.")

dp = Dispatcher()
handler_bot = Bot(token=_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


@dp.callback_query.outer_middleware()
async def _admin_only_callbacks(handler, event: CallbackQuery, data: dict):
    # Defence in depth: inline keyboards are only ever sent to the admin's
    # private chat, but make the boundary structural instead of relying on
    # that invariant (and on non-admin users having no DB row).
    admin_chat_id = getenv("CHAT_ID")
    if event.from_user is None or not admin_chat_id or str(event.from_user.id) != admin_chat_id:
        try:
            await event.answer()
        except Exception:  # noqa: BLE001 — acking a stale query may fail; drop either way
            pass
        return None
    return await handler(event, data)


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


# ---------------------------------------------------------------------------
# Startup work shared by both run modes
# ---------------------------------------------------------------------------

async def _bootstrap() -> None:
    # Idempotently create (or find) the three root databases inside the
    # Channels / Poems / User Quotes pages before any forward is processed.
    # If this fails, every save flow downstream is broken — better to abort
    # startup with a clear log line than serve updates while save_post 400s.
    await notion_api.bootstrap_root_databases()


async def _health(_request: web.Request) -> web.Response:
    """Unauthenticated liveness probe for Caddy / Docker HEALTHCHECK / uptime
    pings: GET /health -> 200 'ok'. Mounted only in webhook mode."""
    return web.Response(text="ok")


# ---------------------------------------------------------------------------
# Run modes — chosen by RUN_MODE (polling = local dev, webhook = production).
# ---------------------------------------------------------------------------

async def run_polling() -> None:
    await _bootstrap()
    asyncio.create_task(sync_service.auto_sync_loop())
    # Drop any webhook a previous production deploy registered, otherwise
    # Telegram keeps delivering by webhook and long polling errors with 409.
    await handler_bot.delete_webhook(drop_pending_updates=False)
    _log.info("Starting in POLLING mode")
    await dp.start_polling(handler_bot)


def run_webhook() -> None:
    # Fail closed: aiogram treats an empty secret_token as "accept every POST",
    # so a missing WEBHOOK_SECRET would let anyone inject updates. Refuse to run.
    secret = getenv("WEBHOOK_SECRET", "").strip()
    if not secret:
        _log.error("RUN_MODE=webhook requires WEBHOOK_SECRET to be set; refusing to start.")
        sys.exit(1)

    host = getenv("WEBHOOK_HOST", "notion-saved-message.xasanboy.dev").strip()
    port = int(getenv("PORT", "8080"))
    # The path is deliberately static. The secret used to be embedded in it,
    # but request paths end up in aiohttp's access log, Caddy's logs, and the
    # startup log line — and since the same value is also the
    # X-Telegram-Bot-Api-Secret-Token header, leaking the path meant leaking
    # the ability to forge updates. Authentication now lives ONLY in the
    # header, which aiogram validates on every request and which no access
    # log records.
    webhook_path = "/webhook"
    webhook_url = f"https://{host}{webhook_path}"

    async def on_startup(app: web.Application) -> None:
        await _bootstrap()
        app["sync_task"] = asyncio.create_task(sync_service.auto_sync_loop())
        await handler_bot.set_webhook(
            webhook_url,
            secret_token=secret,
            drop_pending_updates=True,
        )
        _log.info("Webhook set to %s", webhook_url)

    async def on_shutdown(app: web.Application) -> None:
        task = app.get("sync_task")
        if task is not None:
            task.cancel()
        await handler_bot.delete_webhook()
        await handler_bot.session.close()
        await notion_api.close_http_client()
        _log.info("Webhook deleted, resources released")

    app = web.Application()
    app.router.add_get("/health", _health)
    # secret_token makes aiogram validate the X-Telegram-Bot-Api-Secret-Token
    # header on every request — that header is the sole authentication for
    # this endpoint (see the webhook_path comment above).
    SimpleRequestHandler(
        dispatcher=dp, bot=handler_bot, secret_token=secret
    ).register(app, path=webhook_path)
    setup_application(app, dp, bot=handler_bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    _log.info("Starting in WEBHOOK mode on 0.0.0.0:%s (path %s)", port, webhook_path)
    web.run_app(app, host="0.0.0.0", port=port)


def run() -> None:
    """Entry point: pick the mode from RUN_MODE (defaults to polling for local dev)."""
    if getenv("RUN_MODE", "polling").strip().lower() == "webhook":
        run_webhook()
    else:
        asyncio.run(run_polling())
