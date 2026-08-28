from aiogram import Bot
from aiogram.types import CallbackQuery, Message

from bot import keyboards
from bot.model.bot_models import PostDestination, UserPosts
from bot.services import notion_save_service, user_service
from bot.text_utils import esc
from container import services


async def regenerate(callback_query: CallbackQuery, bot: Bot, by_gpt: bool = False, by_claude: bool = False) -> None:
    chat_id, user, post = _resolve_post(callback_query, prefix="REGENERATE_BY_GPT_" if by_gpt else "REGENERATE_BY_CLAUDE_")
    if post is None:
        await _answer(bot, callback_query, text="Post not found.", alert=True)
        return

    try:
        if by_gpt:
            post.gpt_title = await services.gpt_client.get_post_overview(post.post)
            post.is_title_by_gpt = True
        elif by_claude:
            post.claude_title = await services.claude_client.get_post_title(post.post)
            post.is_title_by_claude = True
    except Exception as exc:  # noqa: BLE001
        await _answer(bot, callback_query, text=f"Title generation failed: {exc}"[:200], alert=True)
        return

    post = user_service.save_or_update_post(post)
    await _refresh_post_message(bot, chat_id, callback_query.message.message_id, post)
    await _answer(bot, callback_query)


async def ask_from_ai(callback_query: CallbackQuery, bot: Bot, from_claude: bool = False, from_gpt: bool = False) -> None:
    prefix = "ASK_FROM_CLAUDE_" if from_claude else "ASK_FROM_GPT_"
    chat_id, user, post = _resolve_post(callback_query, prefix=prefix)
    if post is None:
        await _answer(bot, callback_query, text="Post not found.", alert=True)
        return

    try:
        if from_claude:
            post.claude_title = await services.claude_client.get_post_title(post.post)
            post.is_title_by_claude = True
        elif from_gpt:
            post.gpt_title = await services.gpt_client.get_post_overview(post.post)
            post.is_title_by_gpt = True
    except Exception as exc:  # noqa: BLE001
        await _answer(bot, callback_query, text=f"Title generation failed: {exc}"[:200], alert=True)
        return

    post = user_service.save_or_update_post(post)
    await _refresh_post_message(bot, chat_id, callback_query.message.message_id, post)
    await _answer(bot, callback_query)


async def request_custom_title(callback_query: CallbackQuery, bot: Bot) -> None:
    chat_id, user, post = _resolve_post(callback_query, prefix="TITLE_BY_ME_")
    if post is None or user is None:
        await _answer(bot, callback_query, text="Post not found.", alert=True)
        return
    user_service.set_awaiting_title(user, post.message_id)
    await bot.send_message(
        chat_id,
        "Send me your title for that post (reply to anything — the next text message will be used).",
    )
    await _answer(bot, callback_query)


async def receive_custom_title(message: Message, bot: Bot) -> bool:
    """If the admin owes us a custom title for an earlier post, store it. Returns True if handled."""
    chat_id = str(message.chat.id)
    user = user_service.get_user_by_chat_id(chat_id)
    if user is None or not user.awaiting_title_for_message_id:
        return False

    post = user_service.find_post_by_message_id(user, user.awaiting_title_for_message_id)
    if post is None:
        user_service.set_awaiting_title(user, None)
        return False

    title = (message.text or "").strip()
    if not title:
        await bot.send_message(chat_id, "That title was empty — try again.")
        return True

    post.custom_title = title
    post = user_service.save_or_update_post(post)
    user_service.set_awaiting_title(user, None)

    await bot.send_message(
        chat_id,
        f"Saved your custom title: <b>{esc(title)}</b>",
        reply_markup=keyboards.get_forward_message_cbq(forwarded_message_id=post.message_id, post=post),
    )
    return True


async def save_to_notion(callback_query: CallbackQuery, bot: Bot, *, source: str) -> None:
    """source ∈ {'gpt', 'claude', 'custom'}"""
    prefix = {
        "gpt": "SAVE_WITH_GPT_TITLE_",
        "claude": "SAVE_WITH_CLAUDE_TITLE_",
        "custom": "SAVE_WITH_MY_TITLE_",
    }[source]
    chat_id, user, post = _resolve_post(callback_query, prefix=prefix)
    if post is None:
        await _answer(bot, callback_query, text="Post not found.", alert=True)
        return

    title = notion_save_service.pick_title(post, source)
    if not title:
        await _answer(bot, callback_query, text="No title to save with.", alert=True)
        return

    try:
        page_id = await notion_save_service.save_post(post, title)
    except Exception as exc:  # noqa: BLE001
        await bot.send_message(chat_id, f"Saving to Notion failed: {esc(exc)}")
        await _answer(bot, callback_query)
        return

    post.saved_notion_page_id = page_id
    user_service.save_or_update_post(post)

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=callback_query.message.message_id,
            text=(
                _format_post_summary(post)
                + f"\n✅ Saved to <i>{_destination_label(post.destination)}</i> with title: <b>{esc(title)}</b>"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — the save DID succeed; don't let a render hiccup mask that
        if "message is not modified" not in str(exc).lower():
            await bot.send_message(chat_id, f"✅ Saved with title: <b>{esc(title)}</b>")
    await _answer(bot, callback_query)


async def _answer(bot: Bot, callback_query: CallbackQuery, text: str = None, alert: bool = False) -> None:
    """Ack a callback so the button spinner clears. Tolerates double-answers
    and expired queries — an ack must never take the handler down."""
    try:
        await bot.answer_callback_query(callback_query.id, text=text, show_alert=alert)
    except Exception:  # noqa: BLE001
        pass


def _resolve_post(callback_query: CallbackQuery, *, prefix: str):
    # Inline-message callbacks (and some race conditions) deliver `query` without a
    # bound message. Bail out cleanly instead of AttributeError'ing on `.chat`.
    if callback_query.message is None:
        return None, None, None
    chat_id = str(callback_query.message.chat.id)
    user = user_service.get_user_by_chat_id(chat_id)
    if user is None or not callback_query.data:
        return chat_id, user, None
    forward_message_id = callback_query.data.replace(prefix, "")
    post = user_service.find_post_by_message_id(user, forward_message_id)
    return chat_id, user, post


async def _refresh_post_message(bot: Bot, chat_id: str, message_id: int, post: UserPosts) -> None:
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=_format_post_summary(post),
            reply_markup=keyboards.get_forward_message_cbq(forwarded_message_id=post.message_id, post=post),
        )
    except Exception as exc:
        # Regenerating can produce the identical title; re-rendering identical
        # content is a no-op that Telegram reports as an error. Ignore it.
        if "message is not modified" not in str(exc).lower():
            raise


def _format_post_summary(post: UserPosts) -> str:
    if post.source_channel_name:
        suffix = f"@{post.source_channel_username.lower()}" if post.source_channel_username else "UNKNOWN"
        header = f"<b>{esc(post.source_channel_name)}</b> ({esc(suffix)})\n"
    elif post.source_user_name or post.source_user_username:
        suffix = f"@{post.source_user_username}" if post.source_user_username else "hidden"
        header = f"<b>{esc(post.source_user_name or 'Unknown')}</b> ({esc(suffix)})\n"
    else:
        header = ""

    gpt_line = f"GPT generated title: <b>{esc(post.gpt_title)}</b>\n" if post.gpt_title else "GPT generated title: <i>NO TITLE GENERATED</i>\n"
    claude_line = f"CLAUDE generated title: <b>{esc(post.claude_title)}</b>\n" if post.claude_title else "CLAUDE generated title: <i>NO TITLE GENERATED</i>\n"
    custom_line = f"My title: <b>{esc(post.custom_title)}</b>\n" if post.custom_title else ""
    return header + gpt_line + claude_line + custom_line + f"Will save to: <i>{_destination_label(post.destination)}</i>"


def _destination_label(destination: PostDestination) -> str:
    return {
        PostDestination.CHANNEL: "Channels 📺",
        PostDestination.POEM: "Poems 📜",
        PostDestination.USER_QUOTE: "User quotes 🖊",
    }.get(destination, str(destination))


