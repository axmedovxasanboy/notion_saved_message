import sys
from typing import Optional

from aiogram.types import Message

from bot.model.bot_models import User, UserPosts
from bot.repository import bot_repository
from container import services
from exceptions.bot_exceptions import ArgumentsNotConfiguredCorrectlyException


def save_or_update_user(user: User = None, user_msg: Message = None) -> User | None:
    if user is None:
        if user_msg is None:
            raise ArgumentsNotConfiguredCorrectlyException(
                'Both "user" and "user_msg" variable are set to None. Only one should be set to None',
                423, sys._getframe().f_code.co_name,
            )
        chat_id = str(user_msg.chat.id)
        find_user = get_user_by_chat_id(chat_id)
        first_name = user_msg.from_user.first_name
        last_name = user_msg.from_user.last_name
        username = user_msg.from_user.username

        if find_user:
            find_user.first_name = first_name
            find_user.last_name = last_name
            find_user.username = username
            find_user.chat_id = chat_id
            return bot_repository.update_user(find_user)
        new_user = User(username=username, first_name=first_name, last_name=last_name, chat_id=chat_id)
        return bot_repository.save_user(new_user)

    if user_msg is not None:
        raise ArgumentsNotConfiguredCorrectlyException(
            'Both "user" and "user_msg" variable are given. Only one should be set to None',
            423, sys._getframe().f_code.co_name,
        )
    return bot_repository.update_user(user)


def get_user_by_chat_id(chat_id: str) -> User | None:
    return services.db.get_user_by_chat_id(chat_id)


def save_or_update_post(user_post: UserPosts) -> UserPosts:
    return services.db.save_or_update_post(user_post)


def find_post_by_message_id(user: User, message_id: str) -> Optional[UserPosts]:
    for post in user.posts:
        if str(post.message_id) == str(message_id):
            return post
    return None


def set_awaiting_title(user: User, message_id: Optional[str]) -> User:
    user.awaiting_title_for_message_id = message_id
    return bot_repository.update_user(user)


def set_auto_title_ai(user: User, ai: str) -> User:
    if ai not in ("gpt", "claude"):
        raise ValueError(f"Unsupported AI: {ai}")
    user.auto_title_ai = ai
    return bot_repository.update_user(user)


def set_auto_save(user: User, enabled: bool) -> User:
    user.auto_save = bool(enabled)
    return bot_repository.update_user(user)


def set_auto_sync_interval(user: User, minutes: int) -> User:
    user.auto_sync_interval_minutes = max(0, int(minutes))
    return bot_repository.update_user(user)


def set_posts_sort_order(user: User, order: str) -> User:
    if order not in ("desc", "asc"):
        raise ValueError(f"Unsupported sort order: {order}")
    user.posts_sort_order = order
    return bot_repository.update_user(user)


def set_current_channel_page(user: User, page: int) -> User:
    """Remember which page of the paginated channels list the admin is on.
    A no-op when the value matches the existing one (avoids needless DB writes)."""
    page = max(0, int(page))
    if user.current_channel_page == page:
        return user
    user.current_channel_page = page
    return bot_repository.update_user(user)


def get_admin_user() -> Optional[User]:
    import os
    chat_id = os.getenv("CHAT_ID")
    if not chat_id:
        return None
    return get_user_by_chat_id(chat_id)
