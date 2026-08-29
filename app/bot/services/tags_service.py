"""Free-form tags on posts — parsing, normalization and storage.

Tags are LOCAL ONLY. They are never written to Notion and never read back
from it, so `sync_from_notion` can neither create nor clobber them.

Canonical form is lowercase, no leading '#', whitespace collapsed to '_', and
everything outside word characters / '-' dropped. They're displayed back with
a leading '#'.

The byte cap is not cosmetic: a tag is embedded in inline-keyboard callback
data (`TAG_POSTS_{tag}_P{page}`), and Telegram caps callback_data at 64 BYTES,
not characters. A Cyrillic tag costs 2 bytes per character and an emoji 4, so
capping by character count alone would silently produce buttons Telegram
rejects. 40 bytes leaves room for the longest prefix/suffix combination.
"""

import re
from typing import List, Optional

from bot.model.bot_models import UserPosts
from container import services

MAX_TAG_BYTES = 40
MAX_TAGS_PER_POST = 25

# Sent as the whole message to clear a post's tags — same convention the
# "set channel username" prompt already uses.
CLEAR_SENTINEL = "-"


def _clip_to_bytes(value: str, limit: int) -> str:
    """Truncate to `limit` UTF-8 bytes without splitting a character."""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", "ignore")


def _normalize(chunk: str) -> str:
    tag = chunk.strip().lstrip("#").strip().lower()
    tag = re.sub(r"\s+", "_", tag)
    # Keep unicode word characters (letters, digits, '_') and '-'; drop the rest.
    tag = re.sub(r"[^\w\-]", "", tag)
    tag = tag.strip("_-")
    if not tag:
        return ""
    return _clip_to_bytes(tag, MAX_TAG_BYTES).strip("_-")


def parse_tags(raw: Optional[str]) -> List[str]:
    """Turn admin input into a canonical, deduplicated tag list.

    Accepts all three natural styles:
      '#love #history #financial_advice'  -> hashtag separated
      'love, history, financial advice'   -> comma separated (spaces -> '_')
      'love history'                      -> plain whitespace separated

    Returns [] for empty input or the clear sentinel '-'.
    """
    text = (raw or "").strip()
    if not text or text == CLEAR_SENTINEL:
        return []

    if "#" in text:
        chunks = re.split(r"[#,\n]+", text)
    elif "," in text or "\n" in text:
        chunks = re.split(r"[,\n]+", text)
    else:
        chunks = re.split(r"\s+", text)

    out: List[str] = []
    seen = set()
    for chunk in chunks:
        tag = _normalize(chunk)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
        if len(out) >= MAX_TAGS_PER_POST:
            break
    return out


def format_tags(tags: List[str]) -> str:
    """Render a tag list for display: ['a', 'b'] -> '#a #b'."""
    return " ".join(f"#{tag}" for tag in tags)


# ----- Storage ---------------------------------------------------------------

def get_tags(post_id: int) -> List[str]:
    return services.db.get_tags_for_post(post_id)


def get_tags_for_posts(post_ids: List[int]) -> dict:
    return services.db.get_tags_for_posts(post_ids)


def set_tags(post_id: int, tags: List[str]) -> List[str]:
    return services.db.set_tags_for_post(post_id, tags)


def add_tags(post_id: int, tags: List[str]) -> List[str]:
    """Union `tags` into whatever the post already carries (used by post merge,
    so the absorbed post's tags aren't lost with it)."""
    if not tags:
        return get_tags(post_id)
    merged = set(get_tags(post_id)) | set(tags)
    return set_tags(post_id, sorted(merged)[:MAX_TAGS_PER_POST])


def remove_post_tags(post_id: int) -> None:
    services.db.cleanup_post_tags(post_id)


def list_tags() -> List[tuple]:
    """[(tag, post_count), ...] ordered by count desc, then name asc."""
    return services.db.list_tags_with_counts()


def list_posts(tag: str, sort_order: str = "desc") -> List[UserPosts]:
    return services.db.list_posts_for_tag(tag, sort_order=sort_order)
