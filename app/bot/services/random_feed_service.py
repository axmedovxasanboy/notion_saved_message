"""The daily random feed: N random posts pushed to the admin between 09:00 and
21:00 local time, unprompted.

Design notes worth keeping:

* **Timezone.** The window is local (``RANDOM_FEED_TZ``, default
  ``Asia/Tashkent``) while the server clock and every DB datetime stay UTC.
  Measuring the window in UTC on a UTC host would deliver the last two posts
  after the admin's midnight.

* **Stratified times.** The window is cut into ``count`` equal slots and one
  minute is drawn inside each, with a floor on the gap between consecutive
  picks. Drawing ``count`` independent times over the whole window would
  regularly bunch three of them into the same half hour.

* **Deterministic per day.** The schedule is seeded from (local date, chat id),
  so every process that computes it for a given day gets the same times.
  ``random.seed(str)`` hashes with SHA-512 rather than the process-randomized
  ``hash()``, so this holds across restarts and redeploys — which is what stops
  a restart from redrawing the schedule and re-sending a slot. Post *selection*
  still uses the CSPRNG in ``random_service``, so what you receive stays
  unpredictable; only when it arrives is reproducible.

* **No bursts.** A slot whose time passed more than ``_GRACE_MINUTES`` ago is
  skipped, not delivered late. Without that, a bot restarted in the evening
  would fire the whole day's backlog a minute apart.
"""

import asyncio
import logging
import os
import random
from datetime import date, datetime, time, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot

from bot import keyboards
from bot.services import (
    bot_message_service, favorites_service, random_service, user_service,
)
from container import services

_log = logging.getLogger(__name__)

# Local-time bounds of the delivery window.
WINDOW_START = time(9, 0)
WINDOW_END = time(21, 0)

DEFAULT_TZ = "Asia/Tashkent"

# How often the loop re-checks. One minute is the resolution of the schedule.
_POLL_SECONDS = 60
# A slot later than this many minutes is treated as missed and skipped.
_GRACE_MINUTES = 30
# Floor on the gap between two consecutive deliveries.
_MIN_GAP_MINUTES = 45


def feed_timezone() -> ZoneInfo:
    """Resolve RANDOM_FEED_TZ, falling back to UTC rather than crashing the loop."""
    name = (os.getenv("RANDOM_FEED_TZ") or DEFAULT_TZ).strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        _log.error(
            "RANDOM_FEED_TZ=%r is not a known timezone (is tzdata installed?); "
            "falling back to UTC. The daily window will follow server time.", name,
        )
        return ZoneInfo("UTC")


def _window_minutes() -> int:
    start = WINDOW_START.hour * 60 + WINDOW_START.minute
    end = WINDOW_END.hour * 60 + WINDOW_END.minute
    return end - start


def build_schedule(day: date, seed_key: str, count: int) -> List[time]:
    """Return `count` local delivery times for `day`, ascending.

    Deterministic for a given (day, seed_key, count): recomputing it after a
    restart yields the same times, which is what makes the sent-counter safe.
    """
    if count <= 0:
        return []

    span = _window_minutes()
    slot = span / count
    rng = random.Random(f"{day.isoformat()}:{seed_key}:{count}")

    base = WINDOW_START.hour * 60 + WINDOW_START.minute
    minutes: List[int] = []
    previous: Optional[int] = None
    for index in range(count):
        low = int(index * slot)
        # `span - 1` keeps the last pick strictly inside the window (20:59).
        high = min(int((index + 1) * slot) - 1, span - 1)
        if previous is not None:
            low = max(low, previous + _MIN_GAP_MINUTES)
        if low > high:
            # Slots narrower than the minimum gap (very large `count`):
            # fall back to the slot edge rather than drawing from an empty range.
            low = high
        offset = rng.randint(low, high)
        minutes.append(offset)
        previous = offset

    return [
        (datetime.min + timedelta(minutes=base + offset)).time()
        for offset in minutes
    ]


def _minutes_late(slot: time, now: time) -> float:
    """How many minutes `now` is past `slot` (negative if `slot` hasn't come)."""
    to_min = lambda t: t.hour * 60 + t.minute + t.second / 60  # noqa: E731
    return to_min(now) - to_min(slot)


async def _send_one(bot: Bot, chat_id: str) -> bool:
    """Deliver a single random post. Returns True if something was sent."""
    post = random_service.pick_random_post()
    if post is None:
        _log.info("Daily random: archive is empty, nothing to send")
        return False

    user = user_service.get_user_by_chat_id(chat_id)
    is_favorite = user is not None and favorites_service.is_post_favorite(user.id, post.id)
    header = f"🎲 <b>Daily pick</b> — from {random_service.source_description(post)}"

    return await bot_message_service.send_post_message(
        bot, chat_id, post,
        is_favorite=is_favorite,
        header=header,
        reply_markup=keyboards.get_post_detail_keyboard(
            post, is_favorite=is_favorite, include_random=True,
        ),
    )


async def _tick(bot: Bot, chat_id: str) -> None:
    user = services.db.get_user_by_chat_id(chat_id)
    if user is None:
        return
    count = int(user.random_feed_count or 0)
    if count <= 0:
        return

    now_local = datetime.now(feed_timezone())
    today = now_local.date().isoformat()
    now_time = now_local.time()

    if user.random_feed_date != today:
        user.random_feed_date = today
        user.random_feed_sent = 0
        user = user_service.save_or_update_user(user=user)

    schedule = build_schedule(now_local.date(), chat_id, count)
    sent = int(user.random_feed_sent or 0)

    # Consume slots whose moment has passed (bot was down, or it's a fresh
    # start partway through the day). They are skipped, never batch-delivered.
    skipped = 0
    while sent < len(schedule) and _minutes_late(schedule[sent], now_time) > _GRACE_MINUTES:
        sent += 1
        skipped += 1
    if skipped:
        user.random_feed_sent = sent
        user = user_service.save_or_update_user(user=user)
        _log.info("Daily random: skipped %d slot(s) already past for %s", skipped, today)

    if sent >= len(schedule):
        return
    if _minutes_late(schedule[sent], now_time) < 0:
        return  # next slot hasn't come round yet

    # Advance the counter FIRST. A send that fails is not worth retrying every
    # minute for half an hour, and losing one post beats spamming the chat.
    user.random_feed_sent = sent + 1
    user_service.save_or_update_user(user=user)

    delivered = await _send_one(bot, chat_id)
    _log.info(
        "Daily random: slot %d/%d at %s local -> %s",
        sent + 1, len(schedule), schedule[sent].strftime("%H:%M"),
        "sent" if delivered else "not delivered",
    )


async def random_feed_loop(bot: Bot) -> None:
    """Background task: push the day's random posts at their scheduled times.

    Reads the count fresh each cycle, so changing it in Settings takes effect
    without a restart. Errors are logged and the loop keeps running.
    """
    chat_id = os.getenv("CHAT_ID")
    if not chat_id:
        _log.warning("Daily random: CHAT_ID is not set; feed disabled")
        return

    tz = feed_timezone()
    _log.info(
        "Daily random feed active: window %s–%s %s",
        WINDOW_START.strftime("%H:%M"), WINDOW_END.strftime("%H:%M"), tz.key,
    )

    # Let startup (DB migration, Notion bootstrap) settle first.
    await asyncio.sleep(20)

    while True:
        try:
            await _tick(bot, chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bad tick must not kill the loop
            _log.error("Daily random: tick failed: %s", exc, exc_info=True)
        await asyncio.sleep(_POLL_SECONDS)
