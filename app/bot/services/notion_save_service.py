import os
from typing import Optional

from bot.model.bot_models import PostDestination, UserPosts
from bot.services import channel_service
from container import services
from notion import notion_service


def detect_destination(text: str, forward_origin) -> PostDestination:
    if _looks_like_poem(text):
        return PostDestination.POEM
    if forward_origin is not None and getattr(forward_origin, "type", None) == "channel":
        return PostDestination.CHANNEL
    return PostDestination.USER_QUOTE


def _looks_like_poem(text: str) -> bool:
    lines = [l for l in text.split("\n") if l.strip()]
    if len(lines) < 4:
        return False
    short_lines = sum(1 for l in lines if len(l) <= 80)
    if short_lines / len(lines) < 0.85:
        return False
    avg_len = sum(len(l) for l in lines) / len(lines)
    return avg_len < 60


def pick_title(post: UserPosts, choice: str) -> Optional[str]:
    if choice == "gpt":
        return post.gpt_title
    if choice == "claude":
        return post.claude_title
    if choice == "custom":
        return post.custom_title
    return None


async def save_post(post: UserPosts, title: str) -> str:
    """Persist the post as a row in the destination Notion database. Returns the new row's page id."""
    database_id, properties = await _resolve_target(post, title)
    page_id = await notion_service.create_database_row(database_id, properties, body=post.post)
    post.saved_title = title
    return page_id


async def _resolve_target(post: UserPosts, title: str) -> tuple[str, dict]:
    """Return (target_database_id, properties_payload) for the given post."""
    if post.destination == PostDestination.POEM:
        db_id = notion_service.root_database_id(_require_env("NOTION_POEMS_PAGE_ID"))
        source = post.source_user_name or post.source_user_username or post.source_channel_name or ""
        properties = {
            **notion_service.title_prop(notion_service.PROP_TITLE, title),
            **notion_service.date_prop(notion_service.PROP_POSTED_AT, post.original_post_date),
            **notion_service.rich_text_prop(notion_service.PROP_SOURCE, source or None),
        }
        return db_id, properties

    if post.destination == PostDestination.CHANNEL:
        # ensure_notion_page now returns the per-channel Posts database id (not a page id)
        if post.channel_id is None:
            raise RuntimeError("Channel post is missing channel_id; cannot route to Notion.")
        channel = services.db.get_channel_by_id(post.channel_id)
        if channel is None:
            raise RuntimeError(f"Channel {post.channel_id} not found in DB.")
        posts_db_id = await channel_service.ensure_notion_page(channel)
        properties = {
            **notion_service.title_prop(notion_service.PROP_TITLE, title),
            **notion_service.date_prop(notion_service.PROP_POSTED_AT, post.original_post_date),
        }
        return posts_db_id, properties

    # USER_QUOTE
    db_id = notion_service.root_database_id(_require_env("NOTION_USER_QUOTES_PAGE_ID"))
    source = post.source_user_name or post.source_user_username or "Unknown"
    properties = {
        **notion_service.title_prop(notion_service.PROP_TITLE, title),
        **notion_service.date_prop(notion_service.PROP_POSTED_AT, post.original_post_date),
        **notion_service.rich_text_prop(notion_service.PROP_SOURCE, source),
    }
    return db_id, properties


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set in the environment")
    return value
