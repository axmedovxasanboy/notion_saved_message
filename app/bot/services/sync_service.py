import asyncio
import logging
import os
from datetime import datetime
from typing import Awaitable, Callable, Optional

from bot.model.bot_models import Channel, PostDestination, UserPosts
from container import services
from notion import notion_service

_log = logging.getLogger(__name__)
_AUTO_SYNC_POLL_SECONDS = 60  # how often we re-check the user's interval setting

ProgressCallback = Optional[Callable[[str], Awaitable[None]]]


def _channels_root() -> str:
    return _require_root_env("NOTION_CHANNEL_POSTS_PAGE_ID")


def _poems_root() -> str:
    return _require_root_env("NOTION_POEMS_PAGE_ID")


def _user_quotes_root() -> str:
    return _require_root_env("NOTION_USER_QUOTES_PAGE_ID")


def _require_root_env(name: str) -> str:
    page_id = os.getenv(name)
    if not page_id:
        raise RuntimeError(f"{name} is not set in the environment")
    return page_id


def _admin_user_id() -> int:
    chat_id = os.getenv("CHAT_ID")
    if not chat_id:
        raise RuntimeError("CHAT_ID is not set in the environment")
    user = services.db.get_user_by_chat_id(chat_id)
    if user is None:
        raise RuntimeError("Admin user not found in DB. Send /start to the bot once before syncing.")
    return user.id


async def sync_from_notion(progress: ProgressCallback = None) -> dict:
    """Pull all three Notion databases (Channels + per-channel Posts, Poems, User Quotes)
    into the local DB. Additive only: never deletes DB rows that no longer exist in
    Notion. Channels are matched by `notion_page_id` (with `external_id` fallback so a
    locally-created channel that's mirrored to Notion later doesn't create a duplicate);
    posts by `saved_notion_page_id`."""
    admin_id = _admin_user_id()
    counts = {
        "channels_seen": 0, "new_channels": 0,
        "posts_seen": 0, "new_posts": 0,
        "updated": 0,
    }

    await _sync_channels(admin_id, counts, progress)
    await _sync_flat_database(
        notion_service.root_database_id(_poems_root()),
        destination=PostDestination.POEM,
        admin_id=admin_id, counts=counts, progress=progress, label="Poems",
    )
    await _sync_flat_database(
        notion_service.root_database_id(_user_quotes_root()),
        destination=PostDestination.USER_QUOTE,
        admin_id=admin_id, counts=counts, progress=progress, label="User quotes",
    )

    return counts


async def _sync_channels(
    admin_id: int,
    counts: dict,
    progress: ProgressCallback,
) -> None:
    """Pull the Channels Index database and each channel's nested Posts database."""
    channels_index_db_id = notion_service.root_database_id(_channels_root())
    channel_rows = await notion_service.query_database(
        channels_index_db_id,
        sorts=[{"property": notion_service.PROP_NAME, "direction": "ascending"}],
    )

    total = len(channel_rows)
    for index, row in enumerate(channel_rows, start=1):
        page_id = row["id"]
        name = _read_title(row, notion_service.PROP_NAME) or "Unknown"
        username_raw = _read_rich_text(row, notion_service.PROP_USERNAME)
        username = username_raw[1:] if username_raw and username_raw.startswith("@") else username_raw
        external_id = (
            _read_rich_text(row, notion_service.PROP_EXTERNAL_ID)
            or f"notion_{page_id.replace('-', '')[:10]}"
        )

        if progress is not None:
            await progress(f"Channels ({index}/{total}) {name}")

        channel = _resolve_local_channel(page_id, external_id)
        if channel is None:
            channel = services.db.save_channel(Channel(
                name=name,
                username=username or None,
                external_id=external_id,
                notion_page_id=page_id,
            ))
            counts["new_channels"] += 1
        else:
            changed = False
            if name and channel.name != name:
                channel.name = name; changed = True
            if (channel.username or None) != (username or None):
                channel.username = username or None; changed = True
            if not channel.notion_page_id:
                # First time we're learning this channel's Notion row id (matched via external_id).
                channel.notion_page_id = page_id; changed = True
            if changed:
                channel = services.db.update_channel(channel)
                counts["updated"] += 1
        counts["channels_seen"] += 1

        # Lazily discover (and cache) the per-channel Posts database id. Channels created
        # in Notion by hand may not have one yet — skip those gracefully.
        if channel.notion_posts_db_id is None:
            posts_db_id = await notion_service.find_database_in_page(
                page_id, notion_service.DB_PER_CHANNEL_POSTS,
            )
            if posts_db_id:
                channel.notion_posts_db_id = posts_db_id
                channel = services.db.update_channel(channel)
        if channel.notion_posts_db_id is None:
            continue

        post_rows = await notion_service.query_database(
            channel.notion_posts_db_id,
            sorts=[{"property": notion_service.PROP_POSTED_AT, "direction": "descending"}],
        )
        for post_row in post_rows:
            await _ingest_post_row(
                post_row,
                destination=PostDestination.CHANNEL,
                admin_id=admin_id,
                counts=counts,
                channel=channel,
            )


async def _sync_flat_database(
    db_id: str,
    *,
    destination: PostDestination,
    admin_id: int,
    counts: dict,
    progress: ProgressCallback,
    label: str,
) -> None:
    """Pull a non-channel database (Poems / User Quotes) where each row is a post directly."""
    rows = await notion_service.query_database(
        db_id,
        sorts=[{"property": notion_service.PROP_POSTED_AT, "direction": "descending"}],
    )
    total = len(rows)
    for index, row in enumerate(rows, start=1):
        title = _read_title(row, notion_service.PROP_TITLE) or ""
        if progress is not None:
            await progress(f"{label} ({index}/{total}) {title[:40]}")
        await _ingest_post_row(
            row,
            destination=destination,
            admin_id=admin_id,
            counts=counts,
            channel=None,
        )


async def _ingest_post_row(
    post_row: dict,
    *,
    destination: PostDestination,
    admin_id: int,
    counts: dict,
    channel: Optional[Channel],
) -> None:
    """Common path: take a Notion page dict, upsert the matching local UserPosts row."""
    post_page_id = post_row["id"]
    counts["posts_seen"] += 1
    title = _read_title(post_row, notion_service.PROP_TITLE) or ""
    posted_at = _read_date(post_row, notion_service.PROP_POSTED_AT)
    source = _read_rich_text(post_row, notion_service.PROP_SOURCE)

    existing = services.db.get_post_by_notion_id(post_page_id)
    if existing is None:
        body = await notion_service.fetch_page_plain_text(post_page_id)
        new_post = UserPosts(
            post=body or title,
            message_id=f"notion_{post_page_id.replace('-', '')[:12]}",
            user_id=admin_id,
            saved_title=title or None,
            saved_notion_page_id=post_page_id,
            destination=destination,
            original_post_date=posted_at,
            channel_id=channel.id if channel is not None else None,
            source_channel_name=channel.name if channel is not None else None,
            source_channel_username=channel.username if channel is not None else None,
            source_user_name=source if channel is None else None,
        )
        services.db.save_or_update_post(new_post)
        counts["new_posts"] += 1
        return

    changed = False
    if title and existing.saved_title != title:
        existing.saved_title = title; changed = True
    if posted_at and existing.original_post_date != posted_at:
        existing.original_post_date = posted_at; changed = True
    if channel is not None and existing.channel_id != channel.id:
        existing.channel_id = channel.id; changed = True
    if channel is not None and existing.source_channel_name != channel.name:
        existing.source_channel_name = channel.name; changed = True
    if channel is None and source and existing.source_user_name != source:
        existing.source_user_name = source; changed = True
    if changed:
        services.db.save_or_update_post(existing)
        counts["updated"] += 1


def _resolve_local_channel(page_id: str, external_id: str) -> Optional[Channel]:
    """Match an incoming Notion channel row to its local row.
    Prefer notion_page_id (set on the local row when we first created the Notion row);
    fall back to external_id so a row created locally and later mirrored to Notion
    doesn't show up as a duplicate."""
    channel = services.db.get_channel_by_notion_id(page_id)
    if channel is not None:
        return channel
    return services.db.get_channel_by_external_id(external_id)


def _read_title(row: dict, prop_name: str) -> Optional[str]:
    prop = row.get("properties", {}).get(prop_name)
    if not prop or prop.get("type") != "title":
        return None
    return "".join(t.get("plain_text", "") for t in prop.get("title", [])) or None


def _read_rich_text(row: dict, prop_name: str) -> Optional[str]:
    prop = row.get("properties", {}).get(prop_name)
    if not prop or prop.get("type") != "rich_text":
        return None
    return "".join(t.get("plain_text", "") for t in prop.get("rich_text", [])) or None


def _read_date(row: dict, prop_name: str) -> Optional[datetime]:
    prop = row.get("properties", {}).get(prop_name)
    if not prop or prop.get("type") != "date":
        return None
    date_obj = prop.get("date") or {}
    start = date_obj.get("start")
    if not start:
        return None
    try:
        # Notion returns ISO 8601 with offset; Python's fromisoformat (3.11+)
        # handles `Z` natively, but we normalize for older interpreters too.
        return datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError:
        return None


async def auto_sync_loop() -> None:
    """Background task: runs sync_from_notion at the user-configured interval.

    Reads the interval fresh each cycle so settings changes take effect without restart.
    Errors are logged and the loop keeps running. The loop sleeps a fixed short window
    (`_AUTO_SYNC_POLL_SECONDS`) when sync is disabled, so toggling it on takes effect
    within a minute. When enabled, it sleeps for the full interval between runs so the
    bot's responsiveness isn't affected.
    """
    chat_id = os.getenv("CHAT_ID")
    if not chat_id:
        return

    # Brief startup delay so the first poll doesn't race the bot's own initialization.
    await asyncio.sleep(10)

    while True:
        interval_minutes = 0
        try:
            user = services.db.get_user_by_chat_id(chat_id)
            if user is not None:
                interval_minutes = int(user.auto_sync_interval_minutes or 0)
        except Exception as exc:  # noqa: BLE001
            _log.error("Auto-sync: failed to read user settings: %s", exc)

        if interval_minutes <= 0:
            await asyncio.sleep(_AUTO_SYNC_POLL_SECONDS)
            continue

        try:
            result = await sync_from_notion()
            _log.info("Auto-sync done: %s", result)
        except Exception as exc:  # noqa: BLE001
            _log.error("Auto-sync error: %s", exc)

        await asyncio.sleep(interval_minutes * 60)
