"""The "Random 🎲" screen — one random post from the whole archive.

Sampling lives in bot.services.random_service; this module is only the UI.
The post is rendered with the standard post-detail keyboard, so everything you
can do to a post from Channels or Favorites (favorite, edit title, tags, move,
merge, delete) works here too, plus a reroll button.

Module is named `random_post` rather than `random` so it can never be confused
with the stdlib module of that name.
"""

import os
from aiogram import Bot
from aiogram.types import CallbackQuery, Message

from bot import keyboards
from bot.model.bot_models import UserPosts
from bot.services import (
    bot_message_service, favorites_service, random_service, user_service,
)

ADMIN_CHAT_ID = os.getenv("CHAT_ID")

_EMPTY_TEXT = (
    "<i>Nothing to show yet — forward a post (or run a sync) and the dice will "
    "have something to land on.</i>"
)


async def open_random(message: Message, bot: Bot) -> None:
    """Reply-keyboard entry point: send a fresh random post as a new message."""
    chat_id = str(message.chat.id)
    if chat_id != ADMIN_CHAT_ID:
        await bot.send_message(chat_id, "Random is admin-only.")
        return

    post = random_service.pick_random_post()
    if post is None:
        await bot.send_message(chat_id, _EMPTY_TEXT)
        return

    user = user_service.get_user_by_chat_id(chat_id)
    is_favorite = user is not None and favorites_service.is_post_favorite(user.id, post.id)

    await bot_message_service.send_post_message(
        bot, chat_id, post,
        is_favorite=is_favorite,
        header=_source_line(post),
        reply_markup=keyboards.get_post_detail_keyboard(
            post, is_favorite=is_favorite, include_random=True,
        ),
    )


async def reroll(query: CallbackQuery, bot: Bot) -> None:
    """RANDOM_POST — replace the current message with another random post."""
    if query.message is None:
        return
    post = random_service.pick_random_post()
    if post is None:
        await bot.answer_callback_query(query.id, text="No posts yet.", show_alert=True)
        return

    chat_id = str(query.message.chat.id)
    user = user_service.get_user_by_chat_id(chat_id)
    is_favorite = user is not None and favorites_service.is_post_favorite(user.id, post.id)

    if not await bot_message_service.render_post_edit(
        bot, query, post,
        is_favorite=is_favorite,
        header=_source_line(post),
        reply_markup=keyboards.get_post_detail_keyboard(
            post, is_favorite=is_favorite, include_random=True,
        ),
    ):
        return
    await bot.answer_callback_query(query.id, text=_source_label(post))


def _source_label(post: UserPosts) -> str:
    return random_service.source_label(post)


def _source_line(post: UserPosts) -> str:
    return f"🎲 From {random_service.source_description(post)}"
