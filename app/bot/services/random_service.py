"""Picking a genuinely random post — "surprise me" over the whole archive.

Three things separate this from a bare `random.choice(all_posts)`:

1. **Entropy source.** `random.SystemRandom` draws from the OS CSPRNG
   (`os.urandom`) instead of Mersenne Twister, and its `choice()` uses
   rejection sampling, so there is no modulo bias and no seed to predict.

2. **Two-stage sampling.** A bucket (channel) is drawn first, then a post
   within it. Uniform-over-posts would be dominated by whichever channel you
   forward most: with one 500-post channel and twenty 5-post channels, 83% of
   rolls would come from that single channel. Two-stage gives every channel an
   equal turn, which is what makes the button feel like it's showing you the
   whole archive. Poems and user quotes (no channel) form one extra bucket, so
   nothing you've saved is unreachable.

3. **No near-repeats.** Independent draws repeat far sooner than intuition
   expects — with 30 posts there's already a ~50% chance of a repeat within
   7 rolls (the birthday problem), which reads as "the random is broken" even
   though it's perfectly uniform. Recently shown posts are therefore excluded
   from the pool until the history window rolls over, the way a music player's
   shuffle does. The window scales with archive size so small archives don't
   exclude themselves into a corner.

History is per-process, not persisted: it's a UX nicety, and losing it on
redeploy costs at most one repeated post.
"""

import logging
from random import SystemRandom
from typing import List, Optional

from bot.text_utils import esc
from container import services

_log = logging.getLogger(__name__)

# OS-backed CSPRNG. Instantiated once; it holds no seed state to reuse.
_rng = SystemRandom()

# Never exclude more than this many posts, however large the archive.
_MAX_HISTORY = 25

# Post ids shown recently, oldest first.
_recent: List[int] = []


def _history_cap(total_posts: int) -> int:
    """How many recent posts to exclude.

    Capped at half the archive so a small collection can't exclude every
    candidate and force a reset on every single press.
    """
    return max(1, min(_MAX_HISTORY, total_posts // 2))


def _bucket_sort_key(key: Optional[int]):
    # `sorted()` can't compare None with int, and we need a stable ordered
    # list to hand to choice(). The un-channeled bucket sorts last.
    return (key is None, key or 0)


def pick_random_post_id() -> Optional[int]:
    """Return a post id: uniform over buckets, then history-aware within one.

    Bucket choice deliberately happens BEFORE the history filter. Filtering
    first would let the window empty a small channel entirely (a 5-post channel
    is fully covered by a 25-post history) and drop it from the draw, handing
    the extra probability to whichever channels are big enough to always have
    a live candidate. Choosing the bucket first keeps every channel exactly
    equally likely no matter its size.

    Returns None only when the archive holds no posts at all.
    """
    buckets = services.db.list_post_ids_grouped_by_channel()
    buckets = {key: ids for key, ids in buckets.items() if ids}
    if not buckets:
        return None

    total = sum(len(ids) for ids in buckets.values())
    last = _recent[-1] if _recent else None

    # Skip any bucket whose ONLY post is the one just shown: picking it would
    # force a visible back-to-back repeat, and an archive with many one-post
    # channels hits that case often enough to make the button look broken.
    # This is the narrowest possible exclusion — a bucket is skipped only when
    # it cannot produce anything else — so bucket uniformity is untouched
    # except for the single draw following that bucket's own selection.
    eligible = {
        key: ids for key, ids in buckets.items() if any(pid != last for pid in ids)
    }
    if not eligible:
        # The whole archive is one post; repeating it is the only option.
        eligible = buckets

    bucket = eligible[_rng.choice(sorted(eligible, key=_bucket_sort_key))]

    excluded = set(_recent)
    candidates = [pid for pid in bucket if pid not in excluded]
    if not candidates:
        # Every post in this bucket is inside the history window. Rather than
        # reroll the bucket (which would reintroduce the size bias), drop only
        # the most recent post. `eligible` guarantees this leaves something.
        candidates = [pid for pid in bucket if pid != last] or list(bucket)

    post_id = _rng.choice(candidates)
    _remember(post_id, total)
    return post_id


def _remember(post_id: int, total_posts: int) -> None:
    if post_id in _recent:
        _recent.remove(post_id)
    _recent.append(post_id)
    cap = _history_cap(total_posts)
    del _recent[:-cap]


def pick_random_post():
    """Fetch a random post row, or None when the archive is empty.

    Tolerates a post disappearing between the id draw and the fetch (a delete
    racing a press) by retrying a bounded number of times.
    """
    for _ in range(5):
        post_id = pick_random_post_id()
        if post_id is None:
            return None
        post = services.db.get_post_by_id(post_id)
        if post is not None:
            return post
        _log.info("Random pick hit a vanished post id %s; retrying", post_id)
    return None


def reset_history() -> None:
    """Drop the no-repeat window (used by tests)."""
    _recent.clear()


# ----- Presentation ----------------------------------------------------------

def source_description(post) -> str:
    """HTML fragment naming where a post came from, e.g.
    '<b>Zafna</b> (@zafna_blog)'. Shared by the Random screen and the daily
    feed so both label a pick identically."""
    from bot.services import channel_service  # local: avoids an import cycle

    if post.channel_id is not None:
        channel = channel_service.get_channel(post.channel_id)
        if channel is not None:
            return f"<b>{esc(channel.name)}</b> ({esc(channel_service.visible_id(channel))})"
    source = post.source_user_name or post.source_user_username
    if source:
        return f"<b>{esc(source)}</b>"
    return "<i>an unknown source</i>"


def source_label(post) -> str:
    """Plain-text origin for a callback toast (no HTML, 200-char Telegram cap)."""
    from bot.services import channel_service  # local: avoids an import cycle

    if post.channel_id is not None:
        channel = channel_service.get_channel(post.channel_id)
        if channel is not None:
            return f"🎲 {channel.name}"[:200]
    if post.source_user_name:
        return f"🎲 {post.source_user_name}"[:200]
    return "🎲 Random post"
