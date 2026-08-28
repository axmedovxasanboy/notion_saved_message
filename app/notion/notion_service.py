import asyncio
import html as html_lib
import logging
import os
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Optional

import httpx
from dotenv import load_dotenv

from notion import notion_repository
from notion.model.notion import (
    NotionAnnotations,
    NotionChildPage,
    NotionPageModel,
    NotionParagraphs,
    NotionText,
)
from exceptions.notion_exceptions import NotionPageIdNotSpecified

_log = logging.getLogger(__name__)


def _log_notion(msg: str) -> None:
    # Goes through stdlib logging so it lands in whatever sink the host configures
    # (systemd journal, Docker stdout, etc.) on the deployed server.
    _log.info("[notion] %s", msg)


load_dotenv()
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_MAIN_PAGE_ID = os.getenv("NOTION_MAIN_PAGE_ID")
# Versions ≥ 2025-09-03 introduced the "data sources" split where DB property
# definitions live on a data source instead of the database itself, which makes
# our `POST /databases` payload's `properties` get silently dropped. We target
# 2022-06-28 (stable, well-documented) and fall back to it if the env value
# is missing or newer than what we support.
_SUPPORTED_NOTION_VERSION = "2022-06-28"
NOTION_VERSION = os.getenv("NOTION_VERSION") or _SUPPORTED_NOTION_VERSION
if NOTION_VERSION >= "2025-09-03":
    logging.getLogger(__name__).warning(
        "NOTION_VERSION=%s uses the data-sources API which this codebase doesn't speak; "
        "forcing %s instead. Set NOTION_VERSION=%s in .env to silence this warning.",
        NOTION_VERSION, _SUPPORTED_NOTION_VERSION, _SUPPORTED_NOTION_VERSION,
    )
    NOTION_VERSION = _SUPPORTED_NOTION_VERSION

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_RICH_TEXT_LIMIT = 2000
NOTION_BLOCKS_PER_REQUEST = 100

# Shared httpx client. Reuses TCP / TLS connections across all Notion API calls
# instead of spinning up a fresh client (and handshake) for every request.
_http_client: Optional[httpx.AsyncClient] = None


def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None

# Database titles visible in Notion. Keep short — they're the headers users see.
DB_CHANNELS_INDEX = "Channels"
DB_POEMS = "Poems"
DB_USER_QUOTES = "User Quotes"
DB_PER_CHANNEL_POSTS = "Posts"

# Property names — central so we don't scatter raw strings across the code.
PROP_NAME = "Name"
PROP_TITLE = "Title"
PROP_POSTED_AT = "Posted At"
PROP_SOURCE = "Source"
PROP_USERNAME = "Username"
PROP_EXTERNAL_ID = "External ID"

CHANNELS_INDEX_PROPERTIES = {
    PROP_NAME: {"title": {}},
    PROP_USERNAME: {"rich_text": {}},
    PROP_EXTERNAL_ID: {"rich_text": {}},
}
PER_CHANNEL_POSTS_PROPERTIES = {
    PROP_TITLE: {"title": {}},
    PROP_POSTED_AT: {"date": {}},
}
POEMS_PROPERTIES = {
    PROP_TITLE: {"title": {}},
    PROP_POSTED_AT: {"date": {}},
    PROP_SOURCE: {"rich_text": {}},
}
USER_QUOTES_PROPERTIES = {
    PROP_TITLE: {"title": {}},
    PROP_POSTED_AT: {"date": {}},
    PROP_SOURCE: {"rich_text": {}},
}

# Resolved at startup by `bootstrap_root_databases()`. Maps each of the three
# root page ids (from .env) to the database id we created or found inside it.
_root_db_cache: dict[str, str] = {}


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _raise_with_body(response: httpx.Response) -> None:
    if response.is_success:
        return
    body = ""
    try:
        body = response.text
    except Exception:
        pass
    raise httpx.HTTPStatusError(
        f"{response.status_code} {response.reason_phrase} from {response.request.url}: {body}",
        request=response.request,
        response=response,
    )


_MAX_ATTEMPTS = 5


async def _request_json(
    method: str, url: str, payload: Optional[dict] = None, *, idempotent: bool = True,
) -> dict:
    """One Notion API call with retry/backoff.

    Notion rate-limits at ~3 requests/second (HTTP 429 with Retry-After);
    without retries a long sync aborts midway the moment it hits the limit.
    Honours Retry-After when present. 4xx errors other than 429 raise
    immediately — they never succeed on retry.

    Retry policy depends on whether replaying the call is safe:
      * 429 — always retried: Notion rejected the request without processing.
      * connect errors — always retried: the request never reached Notion.
      * 5xx / post-send transport errors (read timeout, protocol error) —
        retried ONLY for idempotent calls. A create-page POST or an
        append-children PATCH may have been fully processed before the
        failure surfaced; replaying it would duplicate pages/blocks."""
    delay = 1.0
    for attempt in range(_MAX_ATTEMPTS):
        last = attempt == _MAX_ATTEMPTS - 1
        try:
            response = await _client().request(method, url, headers=_headers(), json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if last:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
            continue
        except httpx.TransportError:
            if last or not idempotent:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
            continue
        if response.status_code == 429 or (response.status_code >= 500 and idempotent):
            if last:
                _raise_with_body(response)
            try:
                retry_after = float(response.headers.get("Retry-After", delay))
            except ValueError:
                retry_after = delay
            await asyncio.sleep(min(max(retry_after, delay), 30.0))
            delay = min(delay * 2, 30.0)
            continue
        _raise_with_body(response)
        return response.json()
    raise RuntimeError("unreachable")  # pragma: no cover


async def _get_json(url: str) -> dict:
    return await _request_json("GET", url)


async def _post_json(url: str, payload: dict, *, idempotent: bool = False) -> dict:
    # POST defaults to non-idempotent (create endpoints); the query endpoint
    # opts back in explicitly.
    return await _request_json("POST", url, payload, idempotent=idempotent)


async def _patch_json(url: str, payload: dict, *, idempotent: bool = True) -> dict:
    # Property/parent PATCHes are safe to replay; append-children PATCHes
    # are not and pass idempotent=False.
    return await _request_json("PATCH", url, payload, idempotent=idempotent)


async def _delete_json(url: str) -> dict:
    return await _request_json("DELETE", url)


async def get_page_contents(notion_page_id=None, use_default=True) -> NotionPageModel:
    page_id = notion_page_id or NOTION_MAIN_PAGE_ID
    if page_id is None or not use_default:
        id_not_specified = NotionPageIdNotSpecified(
            "Notion id is not found or is empty", 422, sys._getframe().f_code.co_name
        )
        notion_repository.save_notion_error_log(id_not_specified)
        raise id_not_specified

    url_main = f"{NOTION_API_BASE}/pages/{page_id}"
    url_page = f"{NOTION_API_BASE}/blocks/{page_id}/children"

    request_page = await _get_json(url_main)
    request_content_json = await _get_json(url_page)

    title, page_type = _get_title_n_type_from_page_json(request_page)
    child_pages, paragraphs = _get_page_content_from_content_json(request_content_json)

    return NotionPageModel(page_id, title, page_type, child_pages, paragraphs)


def get_notion_page_content_fully(notion_page: NotionPageModel):
    pages = notion_page.page
    paragraphs = notion_page.paragraphs
    total_items = len(pages) + len(paragraphs)
    full_text = ""

    content_list = []

    for i in range(total_items):
        order_found = False
        for page in pages:
            if i == page.order:
                content_list.append(page)
                order_found = True
        if not order_found:
            for paragraph in paragraphs:
                if i == paragraph.order:
                    content_list.append(paragraph)

    for content in content_list:
        if isinstance(content, NotionChildPage):
            full_text += f"📃<b>{html_lib.escape(content.title, quote=False)}</b>\n"
        elif isinstance(content, NotionParagraphs):
            for text in content.texts:
                # Notion text is arbitrary — escape it or a literal '<'/'&'
                # in a page makes Telegram reject the whole message.
                plain = html_lib.escape(text.plain_text, quote=False)
                annotation = text.annotation
                if annotation.bold:
                    full_text += f"<b>{plain}</b>"
                elif annotation.italic:
                    full_text += f"<i>{plain}</i>"
                elif annotation.underline:
                    full_text += f"<u>{plain}</u>"
                elif annotation.strike:
                    full_text += f"<s>{plain}</s>"
                else:
                    full_text += plain
            full_text += "\n"

    return full_text


async def move_page_to_database(page_id: str, target_database_id: str) -> None:
    """Move an existing page to be a row inside a database. Properties not present
    in the target database's schema are dropped by Notion; both source and target
    must share the same `title`-typed property name for the title to survive."""
    payload = {"parent": {"type": "database_id", "database_id": target_database_id}}
    await _patch_json(f"{NOTION_API_BASE}/pages/{page_id}", payload)


async def archive_page(page_id: str) -> None:
    """Move the page to Notion's trash. Uses DELETE on the block endpoint, which works
    for any block type (regular pages are also exposed as `child_page` blocks) and is
    accepted by every recent Notion-Version including future-dated ones that no longer
    accept the legacy `{archived: true}` PATCH body."""
    await _delete_json(f"{NOTION_API_BASE}/blocks/{page_id}")


async def append_page_blocks(page_id: str, body: str) -> None:
    """Append `body` (Telegram HTML) as new paragraph blocks at the end of an
    existing Notion page. Existing blocks are preserved — this only adds."""
    if not body:
        return
    blocks = _text_to_paragraph_blocks(body)
    while blocks:
        batch = blocks[:NOTION_BLOCKS_PER_REQUEST]
        blocks = blocks[NOTION_BLOCKS_PER_REQUEST:]
        await _patch_json(
            f"{NOTION_API_BASE}/blocks/{page_id}/children", {"children": batch},
            idempotent=False,
        )


# Block types we can round-trip through "read children -> append children".
# Anything else is degraded to a plain paragraph of its text (never dropped
# silently when it has text).
_TRANSFERABLE_BLOCK_TYPES = {
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "quote",
    "to_do", "toggle", "callout", "code", "divider",
}
# Container types whose nested children we also carry over (Notion accepts up
# to two levels of nesting in an append payload).
_NESTABLE_BLOCK_TYPES = {
    "paragraph", "bulleted_list_item", "numbered_list_item",
    "quote", "to_do", "toggle", "callout",
}


def _sanitize_rich_text(items: Optional[list]) -> list:
    """Rebuild API-returned rich_text into a payload Notion accepts on write.

    Read responses carry read-only fields (plain_text, href, ids); echoing
    them back is rejected. Mentions/equations are degraded to their plain
    text so no content is lost."""
    out = []
    for item in items or []:
        plain = item.get("plain_text", "")
        if item.get("type") == "text":
            text_obj = {"content": (item.get("text") or {}).get("content", plain)}
            link = (item.get("text") or {}).get("link") or {}
            if link.get("url"):
                text_obj["link"] = {"url": link["url"]}
        else:
            if not plain:
                continue
            text_obj = {"content": plain}
            if item.get("href"):
                text_obj["link"] = {"url": item["href"]}
        segment = {"type": "text", "text": text_obj}
        annotations = {
            k: v for k, v in (item.get("annotations") or {}).items()
            if v is True and k != "color"
        }
        if annotations:
            segment["annotations"] = annotations
        out.append(segment)
    return out


async def fetch_page_block_payloads(page_id: str, _depth: int = 0) -> tuple:
    """Read a page's blocks and return (write-ready copies, complete).

    Used by the post-merge flow to transfer the REAL content of the page
    being merged away, instead of re-serialising the (possibly truncated)
    local text copy — which used to lose everything past the local cap.

    `complete` is False when any CONTENT could not be carried over: a
    text-free block we can't rebuild (image, file, table, embed, ...), or
    children nested/numbered beyond what an append payload accepts. Callers
    must not archive the source page unless `complete` is True — otherwise
    the untransferred content would land in Notion's trash with no copy
    anywhere. Text-bearing unsupported blocks degraded to plain paragraphs
    do NOT clear the flag: their text survives, only the block type changes."""
    blocks: list = []
    complete = True
    cursor = None
    while True:
        url = f"{NOTION_API_BASE}/blocks/{page_id}/children?page_size=100"
        if cursor:
            url = f"{url}&start_cursor={cursor}"
        data = await _get_json(url)
        for raw in data.get("results", []):
            block_type = raw.get("type")
            payload = raw.get(block_type) or {}
            if block_type not in _TRANSFERABLE_BLOCK_TYPES:
                rich = _sanitize_rich_text(payload.get("rich_text"))
                if rich:
                    blocks.append({
                        "object": "block", "type": "paragraph",
                        "paragraph": {"rich_text": rich},
                    })
                else:
                    # No text to salvage (image, file, table, embed, child
                    # page, ...): the copy would lose this block entirely.
                    complete = False
                if raw.get("has_children"):
                    complete = False
                continue
            clean: dict = {}
            if block_type != "divider":
                clean["rich_text"] = _sanitize_rich_text(payload.get("rich_text"))
                if block_type == "to_do":
                    clean["checked"] = bool(payload.get("checked"))
                if block_type == "code":
                    clean["language"] = payload.get("language") or "plain text"
            if raw.get("has_children"):
                if block_type in _NESTABLE_BLOCK_TYPES and _depth < 1:
                    children, child_complete = await fetch_page_block_payloads(raw["id"], _depth + 1)
                    if not child_complete:
                        complete = False
                    if len(children) > NOTION_BLOCKS_PER_REQUEST:
                        children = children[:NOTION_BLOCKS_PER_REQUEST]
                        complete = False
                    if children:
                        clean["children"] = children
                else:
                    # Children under a code/divider block, or nested deeper
                    # than an append payload allows — they would be lost.
                    complete = False
            blocks.append({"object": "block", "type": block_type, block_type: clean})
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return blocks, complete


def _block_element_count(block: dict) -> int:
    payload = block.get(block.get("type")) or {}
    return 1 + len(payload.get("children") or [])


async def append_block_payloads(page_id: str, blocks: list) -> None:
    """Append pre-built block payloads to a page, batching per API limits.

    Batches count NESTED children too — the API caps a request at 100
    top-level blocks but also ~1000 total block elements, and transferred
    blocks may each carry up to 100 children."""
    _MAX_ELEMENTS_PER_REQUEST = 900
    batch: list = []
    elements = 0
    for block in blocks:
        size = _block_element_count(block)
        if batch and (
            len(batch) >= NOTION_BLOCKS_PER_REQUEST
            or elements + size > _MAX_ELEMENTS_PER_REQUEST
        ):
            await _patch_json(
                f"{NOTION_API_BASE}/blocks/{page_id}/children", {"children": batch},
                idempotent=False,
            )
            batch = []
            elements = 0
        batch.append(block)
        elements += size
    if batch:
        await _patch_json(
            f"{NOTION_API_BASE}/blocks/{page_id}/children", {"children": batch},
            idempotent=False,
        )


def page_url(page_id: Optional[str]) -> Optional[str]:
    if not page_id:
        return None
    return f"https://www.notion.so/{page_id.replace('-', '')}"


# ----- Database CRUD ----------------------------------------------------------

async def find_database_in_page(parent_page_id: str, name: str) -> Optional[str]:
    """Return the id of a `child_database` block under `parent_page_id` whose title matches `name` (case-insensitive)."""
    needle = name.strip().lower()
    cursor = None
    while True:
        url = f"{NOTION_API_BASE}/blocks/{parent_page_id}/children"
        if cursor:
            url = f"{url}?start_cursor={cursor}"
        data = await _get_json(url)
        for block in data.get("results", []):
            if block.get("type") != "child_database":
                continue
            title = block.get("child_database", {}).get("title", "")
            if title.strip().lower() == needle:
                return block["id"]
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return None


async def create_database(parent_page_id: str, title: str, properties: dict) -> str:
    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": title[:NOTION_RICH_TEXT_LIMIT]}}],
        "properties": properties,
    }
    response = await _post_json(f"{NOTION_API_BASE}/databases", payload)
    return response["id"]


async def ensure_database_schema(database_id: str, properties: dict) -> None:
    """Add any missing properties from `properties` to an existing database.

    GETs the current schema first so we PATCH only the *missing* property names.
    This avoids redefining existing properties (which can fail if the type drifts)
    and surfaces a clear list in logs. Re-fetches afterwards to verify Notion
    actually accepted the change — silently ignored PATCHes have been observed."""
    current = await _get_json(f"{NOTION_API_BASE}/databases/{database_id}")
    existing = set((current.get("properties") or {}).keys())
    missing = {name: cfg for name, cfg in properties.items() if name not in existing}
    if not missing:
        return

    _log_notion(f"Adding missing properties {list(missing)} to database {database_id}")
    await _patch_json(
        f"{NOTION_API_BASE}/databases/{database_id}",
        {"properties": missing},
    )

    refreshed = await _get_json(f"{NOTION_API_BASE}/databases/{database_id}")
    refreshed_keys = set((refreshed.get("properties") or {}).keys())
    still_missing = [name for name in missing if name not in refreshed_keys]
    if still_missing:
        raise RuntimeError(
            f"Notion accepted the PATCH but database {database_id} is still missing "
            f"properties {still_missing}. Delete the database in Notion and let the bot "
            f"recreate it on next startup."
        )


async def find_or_create_database(parent_page_id: str, title: str, properties: dict) -> str:
    found = await find_database_in_page(parent_page_id, title)
    if found:
        await ensure_database_schema(found, properties)
        return found
    return await create_database(parent_page_id, title, properties)


async def query_database(
    database_id: str,
    *,
    filter: Optional[dict] = None,
    sorts: Optional[list] = None,
    page_size: int = 100,
) -> list:
    """Query a database, paginating through every page. Returns the raw `results` list."""
    out: list = []
    cursor: Optional[str] = None
    while True:
        payload: dict = {"page_size": page_size}
        if filter is not None:
            payload["filter"] = filter
        if sorts is not None:
            payload["sorts"] = sorts
        if cursor is not None:
            payload["start_cursor"] = cursor
        data = await _post_json(
            f"{NOTION_API_BASE}/databases/{database_id}/query", payload, idempotent=True,
        )
        out.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return out


async def create_database_row(
    database_id: str,
    properties: dict,
    body: Optional[str] = None,
) -> str:
    """Create a page (row) inside a database. `body` (optional) becomes paragraph blocks."""
    blocks = _text_to_paragraph_blocks(body) if body else []
    payload: dict = {
        "parent": {"type": "database_id", "database_id": database_id},
        "properties": properties,
    }
    if blocks:
        payload["children"] = blocks[:NOTION_BLOCKS_PER_REQUEST]
    response = await _post_json(f"{NOTION_API_BASE}/pages", payload)
    page_id = response["id"]

    remaining = blocks[NOTION_BLOCKS_PER_REQUEST:]
    while remaining:
        batch = remaining[:NOTION_BLOCKS_PER_REQUEST]
        remaining = remaining[NOTION_BLOCKS_PER_REQUEST:]
        await _patch_json(
            f"{NOTION_API_BASE}/blocks/{page_id}/children", {"children": batch},
            idempotent=False,
        )

    return page_id


async def update_page_properties(page_id: str, properties: dict) -> None:
    await _patch_json(f"{NOTION_API_BASE}/pages/{page_id}", {"properties": properties})


# ----- Property builders ------------------------------------------------------

def title_prop(name: str, value: str) -> dict:
    return {name: {"title": [{"type": "text", "text": {"content": (value or "")[:NOTION_RICH_TEXT_LIMIT]}}]}}


def rich_text_prop(name: str, value: Optional[str]) -> dict:
    if not value:
        return {name: {"rich_text": []}}
    return {name: {"rich_text": [{"type": "text", "text": {"content": value[:NOTION_RICH_TEXT_LIMIT]}}]}}


def date_prop(name: str, value: Optional[datetime]) -> dict:
    if value is None:
        return {name: {"date": None}}
    # Notion accepts ISO 8601 with offset; default to UTC for naive datetimes
    # so sort order stays consistent across server timezones.
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return {name: {"date": {"start": aware.astimezone(timezone.utc).isoformat()}}}


# ----- Bootstrap --------------------------------------------------------------

async def bootstrap_root_databases() -> dict:
    """Idempotently ensure the three root databases exist under the configured root pages.

    Resolves cached database ids per root page id. Safe to call on every startup —
    if the databases already exist (matched by title), they're reused; missing schema
    properties are added; and the result is verified before being cached so we never
    return a database id whose schema we know is incomplete.
    """
    channels_root = os.getenv("NOTION_CHANNEL_POSTS_PAGE_ID")
    poems_root = os.getenv("NOTION_POEMS_PAGE_ID")
    quotes_root = os.getenv("NOTION_USER_QUOTES_PAGE_ID")
    missing = [n for n, v in [
        ("NOTION_CHANNEL_POSTS_PAGE_ID", channels_root),
        ("NOTION_POEMS_PAGE_ID", poems_root),
        ("NOTION_USER_QUOTES_PAGE_ID", quotes_root),
    ] if not v]
    if missing:
        raise RuntimeError(f"Missing env var(s): {', '.join(missing)}")

    channels_db = await find_or_create_database(channels_root, DB_CHANNELS_INDEX, CHANNELS_INDEX_PROPERTIES)
    poems_db = await find_or_create_database(poems_root, DB_POEMS, POEMS_PROPERTIES)
    quotes_db = await find_or_create_database(quotes_root, DB_USER_QUOTES, USER_QUOTES_PROPERTIES)

    await _verify_database_schema(channels_db, CHANNELS_INDEX_PROPERTIES, label=DB_CHANNELS_INDEX)
    await _verify_database_schema(poems_db, POEMS_PROPERTIES, label=DB_POEMS)
    await _verify_database_schema(quotes_db, USER_QUOTES_PROPERTIES, label=DB_USER_QUOTES)

    _root_db_cache[channels_root] = channels_db
    _root_db_cache[poems_root] = poems_db
    _root_db_cache[quotes_root] = quotes_db
    _log_notion(
        f"Root databases ready — Channels={channels_db}, Poems={poems_db}, UserQuotes={quotes_db}"
    )
    return dict(_root_db_cache)


async def _verify_database_schema(database_id: str, expected: dict, *, label: str) -> None:
    """Fail loudly if `database_id` is missing any required property."""
    current = await _get_json(f"{NOTION_API_BASE}/databases/{database_id}")
    actual = set((current.get("properties") or {}).keys())
    missing = [p for p in expected if p not in actual]
    if missing:
        hint = ""
        if not actual:
            # An empty schema right after a successful create almost always means
            # the API version is using the data-sources model and silently dropped
            # our properties payload. Point the user at .env first.
            hint = (
                f" Schema is completely empty — this typically means NOTION_VERSION "
                f"in your .env (currently {NOTION_VERSION!r}) is on the data-sources API. "
                f"Set NOTION_VERSION=2022-06-28 and delete this database in Notion "
                f"so the bot can recreate it cleanly on next startup."
            )
        raise RuntimeError(
            f"Notion database '{label}' ({database_id}) is missing properties {missing} "
            f"after bootstrap. Existing properties: {sorted(actual)}.{hint}"
        )


def root_database_id(root_page_id: str) -> str:
    db_id = _root_db_cache.get(root_page_id)
    if not db_id:
        raise RuntimeError(
            f"Root database for {root_page_id} not bootstrapped yet. "
            "Call bootstrap_root_databases() at startup."
        )
    return db_id


async def fetch_page_plain_text(page_id: str, max_chars: int = 4000) -> str:
    """Return concatenated plain text from top-level paragraph blocks of a page."""
    parts: list = []
    cursor = None
    total = 0
    while True:
        url = f"{NOTION_API_BASE}/blocks/{page_id}/children"
        if cursor:
            url = f"{url}?start_cursor={cursor}"
        data = await _get_json(url)
        for block in data.get("results", []):
            if block.get("type") == "paragraph":
                rich = block.get("paragraph", {}).get("rich_text", [])
                line = "".join(r.get("plain_text", "") for r in rich)
                if line:
                    parts.append(line)
                    total += len(line) + 1
                    if total >= max_chars:
                        break
        if total >= max_chars or not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    text = "\n".join(parts).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def _text_to_paragraph_blocks(html: str) -> list:
    """Turn Telegram HTML into Notion paragraph blocks.

    Telegram's `html_text` uses tags like <b>, <i>, <u>, <s>, <code>, <a href=...> for
    formatting. We map those onto Notion rich_text annotations / links so the saved page
    keeps the formatting instead of showing literal `<b>...</b>`. Plain text without tags
    works too (the parser just emits one annotation-free run).

    Blank-line runs separate paragraphs and are dropped (they would otherwise show up as
    visible empty lines in Notion).
    """
    if not html:
        return []
    parser = _TelegramHtmlToRuns()
    parser.feed(html)
    parser.close()

    blocks = []
    for paragraph_runs in _runs_to_paragraphs(parser.runs):
        rich_text = _runs_to_rich_text(paragraph_runs)
        if not rich_text:
            continue
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": rich_text},
        })
    return blocks


class _TelegramHtmlToRuns(HTMLParser):
    """Walks Telegram-flavored HTML and emits (text, annotations, link) runs."""

    _BOLD_TAGS = {"b", "strong"}
    _ITALIC_TAGS = {"i", "em"}
    _UNDERLINE_TAGS = {"u", "ins"}
    _STRIKE_TAGS = {"s", "strike", "del"}
    _CODE_TAGS = {"code", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.runs: list = []
        self._bold = 0
        self._italic = 0
        self._underline = 0
        self._strike = 0
        self._code = 0
        self._link_stack: list = []

    def handle_starttag(self, tag, attrs):
        if tag in self._BOLD_TAGS:
            self._bold += 1
        elif tag in self._ITALIC_TAGS:
            self._italic += 1
        elif tag in self._UNDERLINE_TAGS:
            self._underline += 1
        elif tag in self._STRIKE_TAGS:
            self._strike += 1
        elif tag in self._CODE_TAGS:
            self._code += 1
        elif tag == "a":
            self._link_stack.append(dict(attrs).get("href"))
        elif tag == "br":
            self.runs.append(("\n", self._annotations(), self._current_link()))

    def handle_endtag(self, tag):
        if tag in self._BOLD_TAGS:
            self._bold = max(0, self._bold - 1)
        elif tag in self._ITALIC_TAGS:
            self._italic = max(0, self._italic - 1)
        elif tag in self._UNDERLINE_TAGS:
            self._underline = max(0, self._underline - 1)
        elif tag in self._STRIKE_TAGS:
            self._strike = max(0, self._strike - 1)
        elif tag in self._CODE_TAGS:
            self._code = max(0, self._code - 1)
        elif tag == "a" and self._link_stack:
            self._link_stack.pop()

    def handle_data(self, data):
        if data:
            self.runs.append((data, self._annotations(), self._current_link()))

    def _annotations(self) -> dict:
        a = {}
        if self._bold:
            a["bold"] = True
        if self._italic:
            a["italic"] = True
        if self._underline:
            a["underline"] = True
        if self._strike:
            a["strikethrough"] = True
        if self._code:
            a["code"] = True
        return a

    def _current_link(self):
        for link in reversed(self._link_stack):
            if link:
                return link
        return None


def _runs_to_paragraphs(runs):
    """Split a flat run sequence into paragraph-grouped runs on blank-line boundaries."""
    paragraphs: list = [[]]
    for text, annotations, link in runs:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        parts = re.split(r"\n\s*\n+", normalized)
        for i, part in enumerate(parts):
            if i > 0:
                paragraphs.append([])
            if part:
                paragraphs[-1].append((part, annotations, link))

    cleaned = []
    for paragraph_runs in paragraphs:
        trimmed = _trim_paragraph(paragraph_runs)
        if trimmed:
            cleaned.append(trimmed)
    return cleaned


def _trim_paragraph(paragraph_runs):
    if not paragraph_runs:
        return []
    runs = list(paragraph_runs)
    while runs:
        text, ann, link = runs[0]
        stripped = text.lstrip()
        if stripped:
            runs[0] = (stripped, ann, link)
            break
        runs.pop(0)
    while runs:
        text, ann, link = runs[-1]
        stripped = text.rstrip()
        if stripped:
            runs[-1] = (stripped, ann, link)
            break
        runs.pop()
    return runs


def _runs_to_rich_text(paragraph_runs):
    rich_text = []
    for text, annotations, link in paragraph_runs:
        if not text:
            continue
        for chunk in _chunk_text(text, NOTION_RICH_TEXT_LIMIT):
            if not chunk:
                continue
            segment = {"type": "text", "text": {"content": chunk}}
            if link:
                segment["text"]["link"] = {"url": link}
            if annotations:
                segment["annotations"] = dict(annotations)
            rich_text.append(segment)
    return rich_text


def _chunk_text(text: str, size: int):
    if not text:
        return
    for i in range(0, len(text), size):
        yield text[i:i + size]


def _get_title_n_type_from_page_json(json):
    properties = json.get("properties", {})
    for key in ("title", "Name"):
        prop = properties.get(key)
        if not prop:
            continue
        title_items = prop.get("title", [])
        if not title_items:
            continue
        return title_items[0]["plain_text"], prop.get("type", "text").upper()
    return "(untitled)", "TEXT"


def _get_page_content_from_content_json(json):
    results = json.get("results", [])
    child_pages = []
    paragraphs = []
    index = 0
    for r in results:
        block_type = r.get("type")
        if block_type == "child_page":
            child_page = NotionChildPage(r["id"], r["child_page"]["title"], index)
            child_pages.append(child_page)
            index += 1
        elif block_type == "paragraph":
            paragraph_texts = []
            for paragraph in r["paragraph"].get("rich_text", []):
                annotation = NotionAnnotations(
                    bold=paragraph["annotations"]["bold"],
                    italic=paragraph["annotations"]["italic"],
                    underline=paragraph["annotations"]["underline"],
                    strike=paragraph["annotations"]["strikethrough"],
                )
                paragraph_text = NotionText(
                    paragraph["type"],
                    paragraph["plain_text"],
                    paragraph["text"].get("link"),
                    annotation,
                )
                paragraph_texts.append(paragraph_text)
            paragraphs.append(NotionParagraphs(paragraph_texts, index))
            index += 1

    return child_pages, paragraphs
