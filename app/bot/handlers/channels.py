import os
from typing import Optional

from aiogram import Bot
from aiogram.types import CallbackQuery, Message

from bot import keyboards
from bot.model.bot_models import BotSteps, Channel, UserPosts
from bot.services import channel_service, user_service
from container import services
from notion import notion_service

ADMIN_CHAT_ID = os.getenv("CHAT_ID")


async def open_sync(message: Message, bot: Bot) -> None:
    from bot.services import sync_service
    chat_id = str(message.chat.id)
    if chat_id != ADMIN_CHAT_ID:
        await bot.send_message(chat_id, "Sync is admin-only.")
        return
    progress_msg = await bot.send_message(chat_id, "Syncing from Notion…")

    async def report(line: str) -> None:
        try:
            await bot.edit_message_text(
                text=f"Syncing from Notion…\n{line}",
                chat_id=chat_id,
                message_id=progress_msg.message_id,
            )
        except Exception:
            # Telegram throttles edits; a missed progress update is harmless.
            pass

    try:
        result = await sync_service.sync_from_notion(progress=report)
    except Exception as exc:  # noqa: BLE001
        await bot.edit_message_text(
            text=f"Sync failed: {exc}",
            chat_id=chat_id,
            message_id=progress_msg.message_id,
        )
        return

    summary = (
        "✅ Sync complete.\n"
        f"Channels seen: {result['channels_seen']} (new: {result['new_channels']})\n"
        f"Posts seen: {result['posts_seen']} (new: {result['new_posts']})\n"
        f"Updated: {result['updated']}"
    )
    await bot.edit_message_text(text=summary, chat_id=chat_id, message_id=progress_msg.message_id)


async def open_channels(message: Message, bot: Bot) -> None:
    chat_id = str(message.chat.id)
    if chat_id != ADMIN_CHAT_ID:
        await bot.send_message(chat_id, "Channels are admin-only.")
        return

    user = user_service.get_user_by_chat_id(chat_id)
    if user is None:
        return

    user.step = BotSteps.CHANNEL
    user_service.save_or_update_user(user=user)

    await _send_channels_list(bot, chat_id)


async def _send_channels_list(
    bot: Bot, chat_id: str,
    edit_message_id: Optional[int] = None,
    page: int = 0,
) -> None:
    channels = channel_service.list_channels()
    if not channels:
        text = "<i>No channels yet. Forward a channel post to create one.</i>"
        if edit_message_id is not None:
            await bot.edit_message_text(text=text, chat_id=chat_id, message_id=edit_message_id)
        else:
            await bot.send_message(chat_id, text)
        return

    page_channels, page, total_pages = keyboards.paginate_channels(channels, page)
    base_index = page * keyboards.CHANNELS_PER_PAGE

    header = "<b>Your channels</b>"
    if total_pages > 1:
        header += f" — page {page + 1}/{total_pages}"
    lines = [header + "\n"]
    for offset, channel in enumerate(page_channels, start=1):
        suffix = f"@{channel.username}" if channel.username else channel.external_id
        post_count = channel_service.count_posts(channel.id)
        link = notion_service.page_url(channel.notion_page_id)
        link_str = f' — <a href="{link}">notion</a>' if link else ""
        lines.append(
            f"{base_index + offset}. <b>{channel.name}</b> ({suffix}) — {post_count} post(s){link_str}"
        )
    text = "\n".join(lines)
    keyboard = keyboards.get_channels_list_keyboard(channels, page=page)

    if edit_message_id is not None:
        await bot.edit_message_text(
            text=text, chat_id=chat_id, message_id=edit_message_id,
            reply_markup=keyboard, disable_web_page_preview=True,
        )
    else:
        await bot.send_message(
            chat_id, text, reply_markup=keyboard, disable_web_page_preview=True,
        )


async def show_channel_list(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None:
        return
    if query.data == "CH_LIST_NOOP":
        # The page-indicator pill in the keyboard is non-interactive; ack so the
        # spinner clears but don't redraw.
        await bot.answer_callback_query(query.id)
        return
    page = _parse_list_page(query.data)
    await _send_channels_list(
        bot, str(query.message.chat.id),
        edit_message_id=query.message.message_id,
        page=page,
    )
    await bot.answer_callback_query(query.id)


def _parse_list_page(data: Optional[str]) -> int:
    if not data or not data.startswith("CH_LIST_PAGE_"):
        return 0
    try:
        return int(data[len("CH_LIST_PAGE_"):])
    except ValueError:
        return 0


async def show_channel(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None or not query.data:
        return
    channel_id = _parse_int(query.data, "CH_VIEW_")
    channel = channel_service.get_channel(channel_id) if channel_id else None
    if channel is None:
        await bot.answer_callback_query(query.id, text="Channel not found.", show_alert=True)
        return

    text = _format_channel_detail(channel)
    await bot.edit_message_text(
        text=text,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
        reply_markup=keyboards.get_channel_detail_keyboard(channel),
        disable_web_page_preview=True,
    )
    await bot.answer_callback_query(query.id)


async def show_channel_posts(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None or not query.data:
        return
    channel_id = _parse_int(query.data, "CH_POSTS_")
    channel = channel_service.get_channel(channel_id) if channel_id else None
    if channel is None:
        await bot.answer_callback_query(query.id, text="Channel not found.", show_alert=True)
        return

    posts = channel_service.list_posts(channel.id)
    if not posts:
        text = _format_channel_detail(channel) + "\n\n<i>No posts yet.</i>"
        await bot.edit_message_text(
            text=text,
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            reply_markup=keyboards.get_channel_detail_keyboard(channel),
            disable_web_page_preview=True,
        )
    else:
        text = f"<b>Posts in {channel.name}</b> ({len(posts)} total)"
        await bot.edit_message_text(
            text=text,
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            reply_markup=keyboards.get_channel_posts_keyboard(channel, posts),
        )
    await bot.answer_callback_query(query.id)


async def request_rename_channel(query: CallbackQuery, bot: Bot) -> None:
    await _request_input(
        query, bot, prefix="CH_RENAME_",
        action_template="rename_channel:{id}",
        prompt_text="Send the new channel name (next text message is used).",
    )


async def request_set_username(query: CallbackQuery, bot: Bot) -> None:
    await _request_input(
        query, bot, prefix="CH_USERNAME_",
        action_template="set_channel_username:{id}",
        prompt_text="Send the new @username for the channel (or '-' to clear).",
    )


async def request_merge(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None or not query.data:
        return
    channel_id = _parse_int(query.data, "CH_MERGE_")
    channel = channel_service.get_channel(channel_id) if channel_id else None
    if channel is None:
        await bot.answer_callback_query(query.id, text="Channel not found.", show_alert=True)
        return
    others = [c for c in channel_service.list_channels() if c.id != channel.id]
    if not others:
        await bot.answer_callback_query(query.id, text="No other channels to merge into.", show_alert=True)
        return
    await bot.edit_message_text(
        text=f"Merge <b>{channel.name}</b> into which channel?",
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
        reply_markup=keyboards.get_merge_target_keyboard(channel, others),
    )
    await bot.answer_callback_query(query.id)


async def execute_merge(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None or not query.data:
        return
    parts = query.data.replace("CH_MERGE_GO_", "").split("_")
    if len(parts) != 2:
        return
    source = channel_service.get_channel(int(parts[0]))
    target = channel_service.get_channel(int(parts[1]))
    if source is None or target is None:
        await bot.answer_callback_query(query.id, text="Channel not found.", show_alert=True)
        return
    try:
        moved = await channel_service.merge_into(source, target)
    except Exception as exc:  # noqa: BLE001
        await bot.answer_callback_query(query.id, text=f"Merge failed: {exc}", show_alert=True)
        return
    await bot.answer_callback_query(query.id, text=f"Moved {moved} post(s).")
    await _send_channels_list(bot, str(query.message.chat.id), edit_message_id=query.message.message_id)


async def request_delete_channel(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None or not query.data:
        return
    channel_id = _parse_int(query.data, "CH_DELETE_")
    channel = channel_service.get_channel(channel_id) if channel_id else None
    if channel is None:
        await bot.answer_callback_query(query.id, text="Channel not found.", show_alert=True)
        return
    posts = channel_service.count_posts(channel.id)
    await bot.edit_message_text(
        text=(
            f"⚠️ Delete <b>{channel.name}</b>? This archives the Notion page and "
            f"all {posts} post(s) under it."
        ),
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
        reply_markup=keyboards.get_delete_confirm_keyboard(channel),
    )
    await bot.answer_callback_query(query.id)


async def execute_delete_channel(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None or not query.data:
        return
    channel_id = _parse_int(query.data, "CH_DELETE_GO_")
    channel = channel_service.get_channel(channel_id) if channel_id else None
    if channel is None:
        await bot.answer_callback_query(query.id, text="Channel not found.", show_alert=True)
        return
    try:
        await channel_service.delete_channel(channel)
    except Exception as exc:  # noqa: BLE001
        await bot.answer_callback_query(query.id, text=f"Delete failed: {exc}", show_alert=True)
        return
    await bot.answer_callback_query(query.id, text="Deleted.")
    await _send_channels_list(bot, str(query.message.chat.id), edit_message_id=query.message.message_id)


async def show_post(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None or not query.data:
        return
    post_id = _parse_int(query.data, "POST_VIEW_")
    post = services.db.get_post_by_id(post_id) if post_id else None
    if post is None:
        await bot.answer_callback_query(query.id, text="Post not found.", show_alert=True)
        return
    await bot.edit_message_text(
        text=_format_post_detail(post),
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
        reply_markup=keyboards.get_post_detail_keyboard(post),
        disable_web_page_preview=True,
    )
    await bot.answer_callback_query(query.id)


async def request_post_title(query: CallbackQuery, bot: Bot) -> None:
    await _request_input(
        query, bot, prefix="POST_TITLE_",
        action_template="edit_post_title:{id}",
        prompt_text="Send the new title for this post (next text message is used).",
    )


async def request_post_move(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None or not query.data:
        return
    post_id = _parse_int(query.data, "POST_MOVE_")
    post = services.db.get_post_by_id(post_id) if post_id else None
    if post is None:
        await bot.answer_callback_query(query.id, text="Post not found.", show_alert=True)
        return
    others = [c for c in channel_service.list_channels() if c.id != post.channel_id]
    if not others:
        await bot.answer_callback_query(query.id, text="No other channels to move into.", show_alert=True)
        return
    await bot.edit_message_text(
        text="Move post to which channel?",
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
        reply_markup=keyboards.get_post_move_keyboard(post, others),
    )
    await bot.answer_callback_query(query.id)


async def execute_post_move(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None or not query.data:
        return
    parts = query.data.replace("POST_MOVE_GO_", "").split("_")
    if len(parts) != 2:
        return
    post = services.db.get_post_by_id(int(parts[0]))
    target = channel_service.get_channel(int(parts[1]))
    if post is None or target is None:
        await bot.answer_callback_query(query.id, text="Not found.", show_alert=True)
        return
    try:
        post = await channel_service.move_post(post, target)
    except Exception as exc:  # noqa: BLE001
        await bot.answer_callback_query(query.id, text=f"Move failed: {exc}", show_alert=True)
        return
    await bot.answer_callback_query(query.id, text="Moved.")
    await bot.edit_message_text(
        text=_format_post_detail(post),
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
        reply_markup=keyboards.get_post_detail_keyboard(post),
        disable_web_page_preview=True,
    )


async def request_delete_post(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None or not query.data:
        return
    post_id = _parse_int(query.data, "POST_DELETE_")
    post = services.db.get_post_by_id(post_id) if post_id else None
    if post is None:
        await bot.answer_callback_query(query.id, text="Post not found.", show_alert=True)
        return
    await bot.edit_message_text(
        text="⚠️ Delete this post? It will also be archived in Notion.",
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
        reply_markup=keyboards.get_post_delete_confirm_keyboard(post),
    )
    await bot.answer_callback_query(query.id)


async def execute_delete_post(query: CallbackQuery, bot: Bot) -> None:
    if query.message is None or not query.data:
        return
    post_id = _parse_int(query.data, "POST_DELETE_GO_")
    post = services.db.get_post_by_id(post_id) if post_id else None
    if post is None:
        await bot.answer_callback_query(query.id, text="Post not found.", show_alert=True)
        return
    channel_id = post.channel_id
    try:
        await channel_service.delete_post(post)
    except Exception as exc:  # noqa: BLE001
        await bot.answer_callback_query(query.id, text=f"Delete failed: {exc}", show_alert=True)
        return
    await bot.answer_callback_query(query.id, text="Deleted.")
    if channel_id is not None:
        channel = channel_service.get_channel(channel_id)
        if channel is not None:
            posts = channel_service.list_posts(channel.id)
            if posts:
                await bot.edit_message_text(
                    text=f"<b>Posts in {channel.name}</b> ({len(posts)} total)",
                    chat_id=query.message.chat.id,
                    message_id=query.message.message_id,
                    reply_markup=keyboards.get_channel_posts_keyboard(channel, posts),
                )
                return
            await bot.edit_message_text(
                text=_format_channel_detail(channel) + "\n\n<i>No posts yet.</i>",
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                reply_markup=keyboards.get_channel_detail_keyboard(channel),
                disable_web_page_preview=True,
            )
            return
    await _send_channels_list(bot, str(query.message.chat.id), edit_message_id=query.message.message_id)


async def handle_text_input(message: Message, bot: Bot) -> bool:
    """Dispatch admin text input for awaiting_action. Returns True if handled."""
    chat_id = str(message.chat.id)
    user = user_service.get_user_by_chat_id(chat_id)
    if user is None or not user.awaiting_action:
        return False

    action, _, target = user.awaiting_action.partition(":")
    text = (message.text or "").strip()

    user.awaiting_action = None
    user_service.save_or_update_user(user=user)

    if action == "rename_channel":
        channel = channel_service.get_channel(int(target)) if target else None
        if channel is None:
            await bot.send_message(chat_id, "Channel not found.")
            return True
        try:
            channel = await channel_service.rename_channel(channel, text)
        except Exception as exc:  # noqa: BLE001
            await bot.send_message(chat_id, f"Rename failed: {exc}")
            return True
        await bot.send_message(
            chat_id, f"Renamed to <b>{channel.name}</b>.",
            reply_markup=keyboards.get_channel_detail_keyboard(channel),
        )
        return True

    if action == "set_channel_username":
        channel = channel_service.get_channel(int(target)) if target else None
        if channel is None:
            await bot.send_message(chat_id, "Channel not found.")
            return True
        new_username = None if text in ("", "-") else text
        try:
            channel = await channel_service.set_channel_username(channel, new_username)
        except Exception as exc:  # noqa: BLE001
            await bot.send_message(chat_id, f"Username update failed: {exc}")
            return True
        label = f"@{channel.username}" if channel.username else "(none)"
        await bot.send_message(
            chat_id, f"Username set to {label}.",
            reply_markup=keyboards.get_channel_detail_keyboard(channel),
        )
        return True

    if action == "edit_post_title":
        post = services.db.get_post_by_id(int(target)) if target else None
        if post is None:
            await bot.send_message(chat_id, "Post not found.")
            return True
        try:
            post = await channel_service.update_post_title(post, text)
        except Exception as exc:  # noqa: BLE001
            await bot.send_message(chat_id, f"Title update failed: {exc}")
            return True
        await bot.send_message(
            chat_id, f"Title updated: <b>{post.saved_title}</b>",
            reply_markup=keyboards.get_post_detail_keyboard(post),
        )
        return True

    return False


async def _request_input(
    query: CallbackQuery, bot: Bot, *, prefix: str, action_template: str, prompt_text: str
) -> None:
    if query.message is None or not query.data:
        return
    target_id = _parse_int(query.data, prefix)
    if target_id is None:
        return
    user = user_service.get_user_by_chat_id(str(query.message.chat.id))
    if user is None:
        return
    user.awaiting_action = action_template.format(id=target_id)
    user_service.save_or_update_user(user=user)
    await bot.send_message(query.message.chat.id, prompt_text)
    await bot.answer_callback_query(query.id)


def _parse_int(data: str, prefix: str) -> Optional[int]:
    raw = data.replace(prefix, "", 1)
    try:
        return int(raw)
    except ValueError:
        return None


def _format_channel_detail(channel: Channel) -> str:
    suffix = f"@{channel.username}" if channel.username else channel.external_id
    post_count = channel_service.count_posts(channel.id)
    link = notion_service.page_url(channel.notion_page_id)
    parts = [
        f"<b>{channel.name}</b>",
        f"id: {suffix}",
        f"posts: {post_count}",
    ]
    if link:
        parts.append(f'<a href="{link}">Open in Notion</a>')
    return "\n".join(parts)


def _format_post_detail(post: UserPosts) -> str:
    title = post.saved_title or post.custom_title or post.gpt_title or post.claude_title or "(untitled)"
    link = notion_service.page_url(post.saved_notion_page_id)
    parts = [f"<b>{title}</b>"]
    if link:
        parts.append(f'<a href="{link}">Open in Notion</a>')
    snippet = (post.post or "").strip()
    if len(snippet) > 300:
        snippet = snippet[:300] + "…"
    if snippet:
        parts.append("")
        parts.append(snippet)
    return "\n".join(parts)
