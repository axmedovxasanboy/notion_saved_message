"""Tag browsing: the "Tags 🏷" screen and the per-tag post list.

Tags themselves are local-only (see bot.services.tags_service); nothing here
talks to Notion. Callback data carries the tag verbatim, which is safe because
tags_service caps a normalized tag at 40 UTF-8 bytes.
"""

import os
from typing import Optional

from aiogram import Bot
from aiogram.types import CallbackQuery, Message

from bot import keyboards
from bot.model.bot_models import UserPosts
from bot.services import tags_service, user_service
from bot.text_utils import esc
from container import services

ADMIN_CHAT_ID = os.getenv("CHAT_ID")

_EMPTY_TEXT = (
    "<b>🏷 Tags</b>\n\n"
    "<i>No tags yet. Open any post, tap 🏷 Tags, and send something like "
    "<code>#history #money</code>.</i>"
)


async def open_tags(message: Message, bot: Bot) -> None:
    chat_id = str(message.chat.id)
    if chat_id != ADMIN_CHAT_ID:
        await bot.send_message(chat_id, "Tags are admin-only.")
        return

    tags = tags_service.list_tags()
    if not tags:
        await bot.send_message(chat_id, _EMPTY_TEXT)
        return
    await bot.send_message(
        chat_id,
        _list_header(tags, page=0),
        reply_markup=keyboards.get_tags_list_keyboard(tags, page=0),
    )


async def show_tag_list(query: CallbackQuery, bot: Bot) -> None:
    """TAG_MENU (open page 0), TAG_LIST_PAGE_<n> (paginate), TAG_LIST_NOOP (pill)."""
    if query.message is None:
        return
    if query.data == "TAG_LIST_NOOP":
        # The page-indicator pill is non-interactive; ack so the spinner clears.
        await bot.answer_callback_query(query.id)
        return

    tags = tags_service.list_tags()
    if not tags:
        await _edit(bot, query, _EMPTY_TEXT, reply_markup=None)
        await bot.answer_callback_query(query.id)
        return

    page = _parse_list_page(query.data)
    _, page, _ = keyboards.paginate_tags(tags, page)
    await _edit(
        bot, query, _list_header(tags, page),
        reply_markup=keyboards.get_tags_list_keyboard(tags, page=page),
    )
    await bot.answer_callback_query(query.id)


async def show_tag_posts(query: CallbackQuery, bot: Bot) -> None:
    """TAG_POSTS_<tag> (open page 0) or TAG_POSTS_<tag>_P<n> (paginate)."""
    if query.message is None or not query.data:
        return
    if query.data == "TAG_POSTS_NOOP":
        await bot.answer_callback_query(query.id)
        return

    tag, requested_page = _parse_tag_posts(query.data)
    if not tag:
        await bot.answer_callback_query(query.id)
        return

    posts = tags_service.list_posts(tag, sort_order=_posts_sort_order(query))
    if not posts:
        # The tag's last post was deleted or retagged while this screen was open.
        await bot.answer_callback_query(
            query.id, text="No posts carry that tag any more.", show_alert=True,
        )
        await show_tag_list(query, bot)
        return

    _, page, total_pages = keyboards.paginate_posts(posts, requested_page or 0)
    header = f"<b>Posts tagged #{esc(tag)}</b> ({len(posts)} total)"
    if total_pages > 1:
        header += f" — page {page + 1}/{total_pages}"
    await _edit(
        bot, query, header,
        reply_markup=keyboards.get_tag_posts_keyboard(tag, posts, page=page),
    )
    await bot.answer_callback_query(query.id)


async def request_post_tags(query: CallbackQuery, bot: Bot) -> None:
    """POST_TAGS_<post_id> — ask for the tag list; the next text message is used."""
    if query.message is None or not query.data:
        return
    post_id = _parse_int(query.data, "POST_TAGS_")
    post = services.db.get_post_by_id(post_id) if post_id else None
    if post is None:
        await bot.answer_callback_query(query.id, text="Post not found.", show_alert=True)
        return

    user = user_service.get_user_by_chat_id(str(query.message.chat.id))
    if user is None:
        await bot.answer_callback_query(query.id)
        return

    user.awaiting_action = f"edit_post_tags:{post.id}"
    # A pending custom-title request would otherwise swallow this input:
    # bot.text_message() checks receive_custom_title() before handle_text_input().
    user.awaiting_title_for_message_id = None
    user_service.save_or_update_user(user=user)

    current = tags_service.get_tags(post.id)
    current_line = (
        f"Current: <b>{esc(tags_service.format_tags(current))}</b>\n\n"
        if current else "This post has no tags yet.\n\n"
    )
    await bot.send_message(
        query.message.chat.id,
        current_line
        + "Send the tags for this post (the next text message replaces them).\n"
        "Any of these work:\n"
        "<code>#love #history #financial_advice</code>\n"
        "<code>love, history, financial advice</code>\n\n"
        f"Send <code>-</code> to clear all tags. Max {tags_service.MAX_TAGS_PER_POST} tags.",
    )
    await bot.answer_callback_query(query.id)


async def apply_tags_input(
    message: Message, bot: Bot, post: UserPosts, raw: str,
) -> None:
    """Store the admin's tag input for `post` and confirm on the detail keyboard."""
    from bot.services import favorites_service

    tags = tags_service.parse_tags(raw)
    stored = tags_service.set_tags(post.id, tags)

    chat_id = str(message.chat.id)
    user = user_service.get_user_by_chat_id(chat_id)
    is_favorite = user is not None and favorites_service.is_post_favorite(user.id, post.id)

    if stored:
        text = f"🏷 Tags saved: <b>{esc(tags_service.format_tags(stored))}</b>"
    elif raw.strip() == tags_service.CLEAR_SENTINEL:
        text = "🏷 Tags cleared."
    else:
        # Input was non-empty but every token normalized away (e.g. "### !!!"),
        # which is treated the same as an explicit clear.
        text = "🏷 No usable tags in that message — this post now has none."

    await bot.send_message(
        chat_id, text,
        reply_markup=keyboards.get_post_detail_keyboard(post, is_favorite=is_favorite),
    )


# ----- helpers ---------------------------------------------------------------

def _list_header(tags: list, page: int) -> str:
    total_pages = max(1, (len(tags) + keyboards.TAGS_PER_PAGE - 1) // keyboards.TAGS_PER_PAGE)
    header = f"<b>🏷 Tags</b> ({len(tags)} in use)"
    if total_pages > 1:
        header += f" — page {page + 1}/{total_pages}"
    return header + "\n\nPick one to see its posts."


async def _edit(bot: Bot, query: CallbackQuery, text: str, *, reply_markup) -> None:
    try:
        await bot.edit_message_text(
            text=text,
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001 — re-tapping a button re-renders identical text
        if "message is not modified" not in str(exc).lower():
            raise


def _parse_list_page(data: Optional[str]) -> int:
    if not data or not data.startswith("TAG_LIST_PAGE_"):
        return 0
    try:
        return int(data[len("TAG_LIST_PAGE_"):])
    except ValueError:
        return 0


def _parse_tag_posts(data: str) -> tuple[Optional[str], Optional[int]]:
    """Parse TAG_POSTS_<tag> or TAG_POSTS_<tag>_P<page>.

    Splitting on the LAST '_P' is unambiguous: tags are normalized to
    lowercase, so an uppercase 'P' can never occur inside one."""
    raw = data[len("TAG_POSTS_"):]
    if "_P" in raw:
        tag_part, page_part = raw.rsplit("_P", 1)
        try:
            return (tag_part or None), int(page_part)
        except ValueError:
            pass
    return (raw or None), None


def _parse_int(data: str, prefix: str) -> Optional[int]:
    raw = data.replace(prefix, "", 1)
    try:
        return int(raw)
    except ValueError:
        return None


def _posts_sort_order(query: CallbackQuery) -> str:
    """The admin's preferred sort direction, shared with the channel post lists."""
    if query.message is None:
        return "desc"
    user = user_service.get_user_by_chat_id(str(query.message.chat.id))
    if user is None:
        return "desc"
    return user.posts_sort_order or "desc"
