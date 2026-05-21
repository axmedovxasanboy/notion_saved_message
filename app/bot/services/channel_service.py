import hashlib
import os
import uuid
from typing import List, Optional

from sqlalchemy.exc import IntegrityError

from datetime import datetime

from bot.model.bot_models import Channel, UserPosts
from bot.services import favorites_service
from container import services
from notion import notion_service


def _channel_posts_root() -> str:
    page_id = os.getenv("NOTION_CHANNEL_POSTS_PAGE_ID")
    if not page_id:
        raise RuntimeError("NOTION_CHANNEL_POSTS_PAGE_ID is not set in the environment")
    return page_id


def _build_external_id(
    username: Optional[str],
    telegram_chat_id: Optional[int],
    display_name: Optional[str] = None,
) -> str:
    if username:
        return f"un_{username.lower()}"
    if telegram_chat_id is not None:
        return f"tg_{telegram_chat_id}"
    if display_name:
        # Hidden users have no id and no username, only a display name. Hashing the name
        # gives us a stable key so the same anonymous sender doesn't produce a fresh row
        # on every forward.
        digest = hashlib.sha1(display_name.strip().lower().encode("utf-8")).hexdigest()[:8]
        return f"hn_{digest}"
    return f"gen_{uuid.uuid4().hex[:8]}"


def visible_id(channel: Channel) -> str:
    """What we display to the admin as the channel's identifier when no @username exists."""
    if channel.username:
        return f"@{channel.username}"
    return channel.external_id


def find_or_create_channel(name: str, username: Optional[str], telegram_chat_id: Optional[int]) -> Channel:
    external_id = _build_external_id(username, telegram_chat_id, name)
    existing = services.db.get_channel_by_external_id(external_id)
    if existing:
        return existing
    channel = Channel(
        name=name or "Unknown channel",
        username=username,
        external_id=external_id,
    )
    try:
        return services.db.save_channel(channel)
    except IntegrityError:
        # Lost a race: two concurrent forwards from the same channel both reached the
        # save. The unique constraint on external_id rejects the second insert; re-read
        # the row the winner created so the caller still gets a real Channel back.
        winner = services.db.get_channel_by_external_id(external_id)
        if winner is not None:
            return winner
        raise


def get_channel(channel_id: int) -> Optional[Channel]:
    return services.db.get_channel_by_id(channel_id)


def list_channels() -> List[Channel]:
    return services.db.list_channels()


def count_posts(channel_id: int) -> int:
    return services.db.count_posts_for_channel(channel_id)


def count_posts_by_channels(channel_ids: list[int]) -> dict[int, int]:
    return services.db.count_posts_by_channels(channel_ids)


def list_posts(channel_id: int, sort_order: str = "desc") -> List[UserPosts]:
    return services.db.list_posts_for_channel(channel_id, sort_order=sort_order)


async def ensure_notion_page(channel: Channel) -> str:
    """Lazily create this channel's row in the Channels Index database and the per-channel
    Posts database inside it. Returns the per-channel Posts database id (which is what
    new posts get created under)."""
    channels_index_db_id = notion_service.root_database_id(_channel_posts_root())

    if channel.notion_page_id is None:
        page_id = await notion_service.create_database_row(
            channels_index_db_id,
            {
                **notion_service.title_prop(notion_service.PROP_NAME, channel.name or "Unknown channel"),
                **notion_service.rich_text_prop(
                    notion_service.PROP_USERNAME,
                    f"@{channel.username}" if channel.username else None,
                ),
                **notion_service.rich_text_prop(notion_service.PROP_EXTERNAL_ID, channel.external_id),
            },
        )
        channel.notion_page_id = page_id
        channel = services.db.update_channel(channel)

    if channel.notion_posts_db_id is None:
        posts_db_id = await notion_service.find_or_create_database(
            channel.notion_page_id,
            notion_service.DB_PER_CHANNEL_POSTS,
            notion_service.PER_CHANNEL_POSTS_PROPERTIES,
        )
        channel.notion_posts_db_id = posts_db_id
        services.db.update_channel(channel)

    return channel.notion_posts_db_id


async def rename_channel(channel: Channel, new_name: str) -> Channel:
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("Channel name cannot be empty")
    channel.name = new_name
    if channel.notion_page_id:
        await notion_service.update_page_properties(
            channel.notion_page_id,
            notion_service.title_prop(notion_service.PROP_NAME, new_name),
        )
    return services.db.update_channel(channel)


async def set_channel_username(channel: Channel, new_username: Optional[str]) -> Channel:
    cleaned = (new_username or "").strip().lstrip("@")
    channel.username = cleaned or None
    if channel.notion_page_id:
        await notion_service.update_page_properties(
            channel.notion_page_id,
            notion_service.rich_text_prop(
                notion_service.PROP_USERNAME,
                f"@{cleaned}" if cleaned else None,
            ),
        )
    return services.db.update_channel(channel)


async def merge_into(source: Channel, target: Channel) -> int:
    """Move all of `source`'s posts into `target` (DB + Notion), then delete `source`."""
    if source.id == target.id:
        return 0
    target_posts_db_id = await ensure_notion_page(target)
    posts = services.db.list_posts_for_channel(source.id)
    for post in posts:
        if post.saved_notion_page_id:
            await notion_service.move_page_to_database(post.saved_notion_page_id, target_posts_db_id)
    moved = services.db.reassign_posts(source.id, target.id)
    if source.notion_page_id:
        await notion_service.archive_page(source.notion_page_id)
    services.db.delete_channel(source)
    return moved


async def delete_channel(channel: Channel) -> None:
    """Archive the channel's row in the Channels Index database (which cascades to its
    per-channel Posts database and all rows underneath) and remove the channel + post
    rows from the local DB. If Notion fails the DB is left untouched so the two stay
    in sync."""
    if channel.notion_page_id:
        await notion_service.archive_page(channel.notion_page_id)
    for post in services.db.list_posts_for_channel(channel.id):
        favorites_service.remove_post_favorites(post.id)
        services.db.delete_post(post)
    favorites_service.remove_channel_favorites(channel.id)
    services.db.delete_channel(channel)


async def move_post(post: UserPosts, target: Channel) -> UserPosts:
    target_posts_db_id = await ensure_notion_page(target)
    if post.saved_notion_page_id:
        await notion_service.move_page_to_database(post.saved_notion_page_id, target_posts_db_id)
    post.channel_id = target.id
    return services.db.save_or_update_post(post)


async def update_post_title(post: UserPosts, new_title: str) -> UserPosts:
    new_title = new_title.strip()
    if not new_title:
        raise ValueError("Title cannot be empty")
    if post.saved_notion_page_id:
        await notion_service.update_page_properties(
            post.saved_notion_page_id,
            notion_service.title_prop(notion_service.PROP_TITLE, new_title),
        )
    post.saved_title = new_title
    post.custom_title = new_title
    return services.db.save_or_update_post(post)


async def delete_post(post: UserPosts) -> None:
    if post.saved_notion_page_id:
        await notion_service.archive_page(post.saved_notion_page_id)
    favorites_service.remove_post_favorites(post.id)
    services.db.delete_post(post)


async def merge_posts(
    kept: UserPosts, target: UserPosts, original_post_date: datetime,
) -> UserPosts:
    """Merge `target` into `kept` — both posts must belong to the same channel.

    The combined body is `kept.post + "\\n\\n" + target.post`; `kept.original_post_date`
    is overwritten with the caller-supplied value (which is what drives the sort).
    On Notion: the kept page's Posted-At property is updated and target's body is
    appended; target's Notion page is archived. The target row is deleted locally
    (and its favorites cleaned up) so the merge result is a single row."""
    if kept.id == target.id:
        raise ValueError("Cannot merge a post with itself")
    if kept.channel_id != target.channel_id:
        raise ValueError("Posts must belong to the same channel to merge")

    kept_body = (kept.post or "").rstrip()
    target_body = (target.post or "").lstrip()
    if kept_body and target_body:
        kept.post = f"{kept_body}\n\n{target_body}"
    else:
        kept.post = kept_body or target_body
    kept.original_post_date = original_post_date

    if kept.saved_notion_page_id:
        await notion_service.update_page_properties(
            kept.saved_notion_page_id,
            notion_service.date_prop(notion_service.PROP_POSTED_AT, original_post_date),
        )
        if target_body:
            await notion_service.append_page_blocks(kept.saved_notion_page_id, target_body)

    if target.saved_notion_page_id:
        await notion_service.archive_page(target.saved_notion_page_id)
    favorites_service.remove_post_favorites(target.id)
    services.db.delete_post(target)
    return services.db.save_or_update_post(kept)
