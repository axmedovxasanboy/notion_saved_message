import os
from typing import Optional

from aiogram import Bot
from aiogram.types import Message
from dotenv import load_dotenv

from bot import keyboards
from bot.model.bot_models import PostDestination, UserPosts
from bot.services import channel_service, notion_save_service, user_service
from container import services

load_dotenv()


async def forward_message(message: Message, bot: Bot) -> None:
    admin_chat_id = os.getenv("CHAT_ID")
    chat_id = str(message.chat.id)

    if admin_chat_id != chat_id:
        # Per project rule: never persist anything from non-admin users.
        await bot.send_message(chat_id, "What can I help you with?")
        return

    user = user_service.get_user_by_chat_id(chat_id)
    if user is None:
        user = user_service.save_or_update_user(user_msg=message)

    text = message.html_text
    if not text:
        return

    forward_origin = message.forward_origin
    name, username, telegram_id = _extract_origin(forward_origin)
    # Original publish time of the forwarded post; present on every MessageOrigin
    # variant. We pass it through to UserPosts.original_post_date so Notion can
    # sort posts by when they were actually published, not when forwarded.
    original_post_date = getattr(forward_origin, "date", None)

    channel = channel_service.find_or_create_channel(
        name=name or "Unknown",
        username=username,
        telegram_chat_id=telegram_id,
    )

    ai_choice = (user.auto_title_ai or "gpt").lower()
    gpt_title = claude_title = None
    try:
        if ai_choice == "claude":
            claude_title = await services.claude_client.get_post_title(text)
        else:
            gpt_title = await services.gpt_client.get_post_overview(text)
    except Exception as exc:  # noqa: BLE001 — surface the failure to the chat instead of crashing the handler
        await bot.send_message(chat_id, f"Title generation failed ({ai_choice}): {exc}")

    user_post = UserPosts(
        post=text,
        message_id=str(message.message_id),
        user_id=user.id,
        is_title_by_gpt=gpt_title is not None,
        is_title_by_claude=claude_title is not None,
        gpt_title=gpt_title,
        claude_title=claude_title,
        source_channel_name=channel.name,
        source_channel_username=channel.username,
        destination=PostDestination.CHANNEL,
        channel_id=channel.id,
        original_post_date=original_post_date,
    )
    user_post = user_service.save_or_update_post(user_post)

    display_id = channel_service.visible_id(channel)
    header = f"<b>{channel.name}</b> ({display_id})\n"

    if user.auto_save:
        title = claude_title or gpt_title
        if not title:
            await bot.send_message(
                chat_id, header + "<i>Auto-save skipped — no title was generated.</i>",
                reply_to_message_id=message.message_id,
                reply_markup=keyboards.get_forward_message_cbq(
                    forwarded_message_id=str(message.message_id), post=user_post,
                ),
            )
            return
        try:
            page_id = await notion_save_service.save_post(user_post, title)
        except Exception as exc:  # noqa: BLE001
            await bot.send_message(chat_id, header + f"Auto-save to Notion failed: {exc}")
            return
        user_post.saved_notion_page_id = page_id
        user_service.save_or_update_post(user_post)
        await bot.send_message(
            chat_id=chat_id,
            text=header + f"✅ Auto-saved with title: <b>{title}</b>",
            reply_to_message_id=message.message_id,
        )
        return

    msg = header
    msg += f"GPT generated title: <b>{gpt_title}</b>\n" if gpt_title else "GPT generated title: <i>NO TITLE GENERATED</i>\n"
    msg += f"CLAUDE generated title: <b>{claude_title}</b>\n" if claude_title else "CLAUDE generated title: <i>NO TITLE GENERATED</i>\n"
    msg += "Will save to: <i>Channels 📺</i>"

    await bot.send_message(
        chat_id=chat_id,
        text=msg,
        reply_to_message_id=message.message_id,
        reply_markup=keyboards.get_forward_message_cbq(
            forwarded_message_id=str(message.message_id),
            post=user_post,
        ),
    )


def _extract_origin(forward_origin) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """Return (display_name, username, telegram_id) for any forward origin type."""
    origin_type = getattr(forward_origin, "type", None)
    if origin_type == "channel":
        chat = forward_origin.chat
        return chat.title, chat.username, getattr(chat, "id", None)
    if origin_type == "user":
        sender = getattr(forward_origin, "sender_user", None)
        if sender is None:
            return None, None, None
        full_name = sender.first_name or ""
        if getattr(sender, "last_name", None):
            full_name = f"{full_name} {sender.last_name}".strip()
        return full_name or None, sender.username, sender.id
    if origin_type == "hidden_user":
        return getattr(forward_origin, "sender_user_name", None), None, None
    if origin_type == "chat":
        chat = getattr(forward_origin, "sender_chat", None)
        if chat is None:
            return None, None, None
        return getattr(chat, "title", None), getattr(chat, "username", None), getattr(chat, "id", None)
    return None, None, None
