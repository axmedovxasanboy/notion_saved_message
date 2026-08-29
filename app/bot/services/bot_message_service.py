"""Shared rendering of a post's detail view, with safe fallbacks.

A stored post body is either Telegram HTML (from a forward's `html_text`) or
plain text (synced from Notion, which may contain raw '<' / '&'). Rendering
therefore has three layered attempts:

1. body as stored          — preserves formatting for forwarded posts;
2. body escaped            — when Telegram rejects the stored body with a
                             parse-entities error (plain Notion text);
3. escaped plain-text preview — when the message exceeds Telegram's 4096-char
                             limit; truncation happens on the *plain text*
                             projection so it can never split a tag or entity.
"""

from typing import Optional

from aiogram import Bot
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from bot.model.bot_models import UserPosts
from bot.services import tags_service
from bot.text_utils import esc, html_to_plain_text, truncate
from notion import notion_service

PREVIEW_CHARS = 500


def format_post_detail(
    post: UserPosts,
    *,
    full: bool = True,
    is_favorite: bool = False,
    escape_body: bool = False,
    include_date: bool = True,
    header: Optional[str] = None,
) -> str:
    title = post.saved_title or post.custom_title or post.gpt_title or post.claude_title or "(untitled)"
    link = notion_service.page_url(post.saved_notion_page_id)
    star = " ⭐" if is_favorite else ""
    # `header` is pre-escaped HTML supplied by the caller (e.g. the Random
    # screen's "from which channel" line); it is not escaped again here.
    parts = [header] if header else []
    parts.append(f"<b>{esc(title)}</b>{star}")
    if link:
        parts.append(f'<a href="{link}">Open in Notion</a>')

    # Tags are read here rather than passed in so the four call sites of
    # render_post_edit() don't each have to fetch them. `post.id` is None only
    # for a post that was never persisted, which can't reach this screen.
    if post.id is not None:
        tags = tags_service.get_tags(post.id)
        if tags:
            parts.append(f"🏷 {esc(tags_service.format_tags(tags))}")

    body = (post.post or "").strip()
    if not full:
        body = esc(truncate(html_to_plain_text(body), PREVIEW_CHARS))
    elif escape_body:
        body = esc(body)
    if body:
        parts.append("")
        parts.append(body)

    if include_date and post.original_post_date is not None:
        parts.append("")
        parts.append(f"<i>Posted: {post.original_post_date.strftime('%Y-%m-%d %H:%M')} UTC</i>")
    return "\n".join(parts)


async def _render_with_fallbacks(deliver, post: UserPosts, *, is_favorite: bool,
                                 include_date: bool,
                                 header: Optional[str] = None) -> tuple:
    """Try progressively safer renderings of `post` until `deliver` accepts one.

    `deliver` is an async callable taking the rendered text. Returns
    (delivered, last_error); `last_error` is None when delivery succeeded or
    when the ladder ran out without Telegram raising."""
    attempts = [{"full": True, "escape_body": False}]
    while attempts:
        kwargs = attempts.pop(0)
        text = format_post_detail(
            post, is_favorite=is_favorite, include_date=include_date,
            header=header, **kwargs
        )
        if not kwargs["full"]:
            link = notion_service.page_url(post.saved_notion_page_id)
            link_str = f' <a href="{link}">Open full post in Notion →</a>' if link else ""
            text += f"\n\n⚠️ <i>Post is too long for Telegram (limit: 4096 characters).{link_str}</i>"
        try:
            await deliver(text)
            return True, None
        except Exception as exc:  # noqa: BLE001 — we branch on Telegram's error text
            err = str(exc).lower()
            if "message is not modified" in err or "message_not_modified" in err:
                return True, None
            if (
                "can't parse entities" in err
                and kwargs["full"]
                and not kwargs["escape_body"]
            ):
                attempts.append({"full": True, "escape_body": True})
                continue
            if ("too long" in err or "message_too_long" in err) and kwargs["full"]:
                attempts.append({"full": False, "escape_body": False})
                continue
            return False, exc
    return False, None


async def render_post_edit(
    bot: Bot,
    query: CallbackQuery,
    post: UserPosts,
    *,
    is_favorite: bool,
    reply_markup: Optional[InlineKeyboardMarkup],
    include_date: bool = True,
    header: Optional[str] = None,
) -> bool:
    """Edit the callback's message to show `post`, degrading gracefully.

    Returns True when some rendering was delivered (or the message was already
    up to date); False when every attempt failed (the callback is answered
    with the error in that case, so callers just return)."""
    async def _edit(text: str) -> None:
        await bot.edit_message_text(
            text=text,
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )

    delivered, exc = await _render_with_fallbacks(
        _edit, post, is_favorite=is_favorite, include_date=include_date, header=header,
    )
    if not delivered and exc is not None:
        # answer_callback_query text is plain text (no parse mode) but is
        # capped at 200 chars by Telegram. Some callers ack before rendering,
        # and a query can only be answered once — tolerate that.
        try:
            await bot.answer_callback_query(query.id, text=f"Error: {exc}"[:200], show_alert=True)
        except Exception:  # noqa: BLE001
            pass
    return delivered


async def send_post_message(
    bot: Bot,
    chat_id: str,
    post: UserPosts,
    *,
    is_favorite: bool,
    reply_markup: Optional[InlineKeyboardMarkup],
    include_date: bool = True,
    header: Optional[str] = None,
) -> bool:
    """Send `post` as a NEW message, using the same degradation ladder as
    render_post_edit. Used by entry points that arrive from a reply-keyboard
    press, where there is no existing message to edit."""
    async def _send(text: str) -> None:
        await bot.send_message(
            chat_id, text, reply_markup=reply_markup, disable_web_page_preview=True,
        )

    delivered, exc = await _render_with_fallbacks(
        _send, post, is_favorite=is_favorite, include_date=include_date, header=header,
    )
    if not delivered and exc is not None:
        await bot.send_message(chat_id, f"Couldn't render that post: {esc(exc)}"[:400])
    return delivered
