import os
from typing import Optional

from aiogram import Bot
from aiogram.types import CallbackQuery, Message

from bot import keyboards
from bot.model.bot_models import Channel, UserPosts
from bot.services import channel_service, favorites_service, user_service
from container import services
from notion import notion_service

ADMIN_CHAT_ID = os.getenv("CHAT_ID")


async def open_favorites(message: Message, bot: Bot) -> None:
    chat_id = str(message.chat.id)
    if chat_id != ADMIN_CHAT_ID:
        await bot.send_message(chat_id, "Favorites are admin-only.")
        return
    user = user_service.get_user_by_chat_id(chat_id)
    if user is None:
        return
    await bot.send_message(
        chat_id,
        _favorites_menu_text(user.id),
        reply_markup=keyboards.get_favorites_menu_keyboard(),
    )


async def show_menu(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None:
        return
    chat_id = str(query.message.chat.id)
    user = user_service.get_user_by_chat_id(chat_id)
    if user is None:
        await bot.answer_callback_query(query.id)
        return
    await bot.edit_message_text(
        text=_favorites_menu_text(user.id),
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
        reply_markup=keyboards.get_favorites_menu_keyboard(),
    )
    await bot.answer_callback_query(query.id)


async def show_favorite_channels(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None:
        return
    user = user_service.get_user_by_chat_id(str(query.message.chat.id))
    if user is None:
        await bot.answer_callback_query(query.id)
        return
    channels = favorites_service.list_favorite_channels(user.id)
    if not channels:
        await bot.edit_message_text(
            text="<i>No favorite channels yet. Open a channel and tap ☆ Favorite to add one.</i>",
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            reply_markup=keyboards.get_favorites_menu_keyboard(),
        )
        await bot.answer_callback_query(query.id)
        return
    await bot.edit_message_text(
        text=f"<b>⭐ Favorite channels</b> ({len(channels)})",
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
        reply_markup=keyboards.get_favorite_channels_keyboard(channels),
    )
    await bot.answer_callback_query(query.id)


async def show_favorite_posts(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None:
        return
    user = user_service.get_user_by_chat_id(str(query.message.chat.id))
    if user is None:
        await bot.answer_callback_query(query.id)
        return
    posts = favorites_service.list_favorite_posts(user.id)
    if not posts:
        await bot.edit_message_text(
            text="<i>No favorite posts yet. Open a post and tap ☆ Favorite to add one.</i>",
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            reply_markup=keyboards.get_favorites_menu_keyboard(),
        )
        await bot.answer_callback_query(query.id)
        return
    await bot.edit_message_text(
        text=f"<b>⭐ Favorite posts</b> ({len(posts)})",
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
        reply_markup=keyboards.get_favorite_posts_keyboard(posts),
    )
    await bot.answer_callback_query(query.id)


async def open_favorite_channel(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None or not query.data:
        return
    channel_id = _parse_int(query.data, "FAV_OPEN_CH_")
    channel = channel_service.get_channel(channel_id) if channel_id else None
    if channel is None:
        await bot.answer_callback_query(query.id, text="Channel not found.", show_alert=True)
        return
    user = user_service.get_user_by_chat_id(str(query.message.chat.id))
    is_favorite = user is not None and favorites_service.is_channel_favorite(user.id, channel.id)
    from_page = user.current_channel_page if user is not None else 0
    await bot.edit_message_text(
        text=_format_channel(channel, is_favorite=is_favorite),
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
        reply_markup=keyboards.get_channel_detail_keyboard(
            channel, from_page=from_page, is_favorite=is_favorite,
        ),
        disable_web_page_preview=True,
    )
    await bot.answer_callback_query(query.id)


async def open_favorite_post(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None or not query.data:
        return
    post_id = _parse_int(query.data, "FAV_OPEN_POST_")
    post = services.db.get_post_by_id(post_id) if post_id else None
    if post is None:
        await bot.answer_callback_query(query.id, text="Post not found.", show_alert=True)
        return
    user = user_service.get_user_by_chat_id(str(query.message.chat.id))
    is_favorite = user is not None and favorites_service.is_post_favorite(user.id, post.id)
    try:
        await bot.edit_message_text(
            text=_format_post(post, full=True, is_favorite=is_favorite),
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            reply_markup=keyboards.get_post_detail_keyboard(post, is_favorite=is_favorite),
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001
        err = str(exc).lower()
        if "message is not modified" in err or "message_not_modified" in err:
            await bot.answer_callback_query(query.id)
            return
        if "too long" in err or "message_too_long" in err or "message is too long" in err:
            link = notion_service.page_url(post.saved_notion_page_id)
            link_str = f' <a href="{link}">Open full post in Notion →</a>' if link else ""
            note = f"\n\n⚠️ <i>Post is too long for Telegram (limit: 4096 characters).{link_str}</i>"
            try:
                await bot.edit_message_text(
                    text=_format_post(post, full=False, is_favorite=is_favorite) + note,
                    chat_id=query.message.chat.id,
                    message_id=query.message.message_id,
                    reply_markup=keyboards.get_post_detail_keyboard(post, is_favorite=is_favorite),
                    disable_web_page_preview=True,
                )
            except Exception as inner_exc:  # noqa: BLE001
                await bot.answer_callback_query(
                    query.id, text=f"Error: {inner_exc}", show_alert=True,
                )
                return
        else:
            await bot.answer_callback_query(query.id, text=f"Error: {exc}", show_alert=True)
            return
    await bot.answer_callback_query(query.id)


async def toggle_channel_favorite(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None or not query.data:
        return
    channel_id = _parse_int(query.data, "FAV_TOGGLE_CH_")
    channel = channel_service.get_channel(channel_id) if channel_id else None
    if channel is None:
        await bot.answer_callback_query(query.id, text="Channel not found.", show_alert=True)
        return
    user = user_service.get_user_by_chat_id(str(query.message.chat.id))
    if user is None:
        await bot.answer_callback_query(query.id)
        return

    now_favorite = favorites_service.toggle_channel_favorite(user.id, channel.id)
    await bot.edit_message_text(
        text=_format_channel(channel, is_favorite=now_favorite),
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
        reply_markup=keyboards.get_channel_detail_keyboard(
            channel, from_page=user.current_channel_page, is_favorite=now_favorite,
        ),
        disable_web_page_preview=True,
    )
    await bot.answer_callback_query(
        query.id, text="Added to favorites." if now_favorite else "Removed from favorites.",
    )


async def toggle_post_favorite(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None or not query.data:
        return
    post_id = _parse_int(query.data, "FAV_TOGGLE_POST_")
    post = services.db.get_post_by_id(post_id) if post_id else None
    if post is None:
        await bot.answer_callback_query(query.id, text="Post not found.", show_alert=True)
        return
    user = user_service.get_user_by_chat_id(str(query.message.chat.id))
    if user is None:
        await bot.answer_callback_query(query.id)
        return

    now_favorite = favorites_service.toggle_post_favorite(user.id, post.id)
    try:
        await bot.edit_message_text(
            text=_format_post(post, full=True, is_favorite=now_favorite),
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            reply_markup=keyboards.get_post_detail_keyboard(post, is_favorite=now_favorite),
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001
        err = str(exc).lower()
        if "too long" in err or "message_too_long" in err or "message is too long" in err:
            link = notion_service.page_url(post.saved_notion_page_id)
            link_str = f' <a href="{link}">Open full post in Notion →</a>' if link else ""
            note = f"\n\n⚠️ <i>Post is too long for Telegram (limit: 4096 characters).{link_str}</i>"
            await bot.edit_message_text(
                text=_format_post(post, full=False, is_favorite=now_favorite) + note,
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                reply_markup=keyboards.get_post_detail_keyboard(post, is_favorite=now_favorite),
                disable_web_page_preview=True,
            )
    await bot.answer_callback_query(
        query.id, text="Added to favorites." if now_favorite else "Removed from favorites.",
    )


def _favorites_menu_text(user_id: int) -> str:
    ch_count = len(favorites_service.list_favorite_channels(user_id))
    post_count = len(favorites_service.list_favorite_posts(user_id))
    return (
        "<b>⭐ Favorites</b>\n\n"
        f"📺 Channels: <b>{ch_count}</b>\n"
        f"📝 Posts: <b>{post_count}</b>\n\n"
        "Pick a type to view."
    )


def _format_channel(channel: Channel, is_favorite: bool = False) -> str:
    suffix = f"@{channel.username}" if channel.username else channel.external_id
    post_count = channel_service.count_posts(channel.id)
    link = notion_service.page_url(channel.notion_page_id)
    star = " ⭐" if is_favorite else ""
    parts = [
        f"<b>{channel.name}</b>{star}",
        f"id: {suffix}",
        f"posts: {post_count}",
    ]
    if link:
        parts.append(f'<a href="{link}">Open in Notion</a>')
    return "\n".join(parts)


def _format_post(post: UserPosts, full: bool = True, is_favorite: bool = False) -> str:
    title = post.saved_title or post.custom_title or post.gpt_title or post.claude_title or "(untitled)"
    link = notion_service.page_url(post.saved_notion_page_id)
    star = " ⭐" if is_favorite else ""
    parts = [f"<b>{title}</b>{star}"]
    if link:
        parts.append(f'<a href="{link}">Open in Notion</a>')
    snippet = (post.post or "").strip()
    if not full and len(snippet) > 500:
        snippet = snippet[:500] + "…"
    if snippet:
        parts.append("")
        parts.append(snippet)
    return "\n".join(parts)


def _parse_int(data: str, prefix: str) -> Optional[int]:
    raw = data.replace(prefix, "", 1)
    try:
        return int(raw)
    except ValueError:
        return None
