from typing import Optional

from aiogram import Bot
from aiogram.types import CallbackQuery, Message

from bot import keyboards
from bot.model.bot_models import PostDestination, User, UserPosts
from bot.services import notion_save_service, user_service
from container import services


async def regenerate(callback_query: CallbackQuery, bot: Bot, by_gpt: bool = False, by_claude: bool = False) -> None:
    chat_id, user, post = _resolve_post(callback_query, prefix="REGENERATE_BY_GPT_" if by_gpt else "REGENERATE_BY_CLAUDE_")
    if post is None:
        return

    if by_gpt:
        post.gpt_title = await services.gpt_client.get_post_overview(post.post)
        post.is_title_by_gpt = True
    elif by_claude:
        post.claude_title = await services.claude_client.get_post_title(post.post)
        post.is_title_by_claude = True

    post = user_service.save_or_update_post(post)
    await _refresh_post_message(bot, chat_id, callback_query.message.message_id, post)


async def ask_from_ai(callback_query: CallbackQuery, bot: Bot, from_claude: bool = False, from_gpt: bool = False) -> None:
    prefix = "ASK_FROM_CLAUDE_" if from_claude else "ASK_FROM_GPT_"
    chat_id, user, post = _resolve_post(callback_query, prefix=prefix)
    if post is None:
        return

    if from_claude:
        post.claude_title = await services.claude_client.get_post_title(post.post)
        post.is_title_by_claude = True
    elif from_gpt:
        post.gpt_title = await services.gpt_client.get_post_overview(post.post)
        post.is_title_by_gpt = True

    post = user_service.save_or_update_post(post)
    await _refresh_post_message(bot, chat_id, callback_query.message.message_id, post)


async def request_custom_title(callback_query: CallbackQuery, bot: Bot) -> None:
    chat_id, user, post = _resolve_post(callback_query, prefix="TITLE_BY_ME_")
    if post is None or user is None:
        return
    user_service.set_awaiting_title(user, post.message_id)
    await bot.send_message(
        chat_id,
        "Send me your title for that post (reply to anything — the next text message will be used).",
    )


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
        f"Saved your custom title: <b>{title}</b>",
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
        return

    title = notion_save_service.pick_title(post, source)
    if not title:
        await bot.answer_callback_query(callback_query.id, text="No title to save with.", show_alert=True)
        return

    try:
        page_id = await notion_save_service.save_post(post, title)
    except Exception as exc:  # noqa: BLE001
        await bot.send_message(chat_id, f"Saving to Notion failed: {exc}")
        return

    post.saved_notion_page_id = page_id
    user_service.save_or_update_post(post)

    await bot.edit_message_text(
        chat_id=chat_id,
        message_id=callback_query.message.message_id,
        text=(
            _format_post_summary(post)
            + f"\n✅ Saved to <i>{_destination_label(post.destination)}</i> with title: <b>{title}</b>"
        ),
    )


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
    await bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=_format_post_summary(post),
        reply_markup=keyboards.get_forward_message_cbq(forwarded_message_id=post.message_id, post=post),
    )


def _format_post_summary(post: UserPosts) -> str:
    if post.source_channel_name:
        suffix = f"@{post.source_channel_username.lower()}" if post.source_channel_username else "UNKNOWN"
        header = f"<b>{post.source_channel_name}</b> ({suffix})\n"
    elif post.source_user_name or post.source_user_username:
        suffix = f"@{post.source_user_username}" if post.source_user_username else "hidden"
        header = f"<b>{post.source_user_name or 'Unknown'}</b> ({suffix})\n"
    else:
        header = ""

    gpt_line = f"GPT generated title: <b>{post.gpt_title}</b>\n" if post.gpt_title else "GPT generated title: <i>NO TITLE GENERATED</i>\n"
    claude_line = f"CLAUDE generated title: <b>{post.claude_title}</b>\n" if post.claude_title else "CLAUDE generated title: <i>NO TITLE GENERATED</i>\n"
    custom_line = f"My title: <b>{post.custom_title}</b>\n" if post.custom_title else ""
    return header + gpt_line + claude_line + custom_line + f"Will save to: <i>{_destination_label(post.destination)}</i>"


def _destination_label(destination: PostDestination) -> str:
    return {
        PostDestination.CHANNEL: "Channels 📺",
        PostDestination.POEM: "Poems 📜",
        PostDestination.USER_QUOTE: "User quotes 🖊",
    }.get(destination, str(destination))


