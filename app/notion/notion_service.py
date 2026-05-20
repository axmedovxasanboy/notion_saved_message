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
from notion.model.notion import *
from exceptions.notion_exceptions import *

_log = logging.getLogger(__name__)


def _log_notion(msg: str) -> None:
    # Goes through stdlib logging so it lands in whatever sink the host configures
    # (systemd journal, Docker stdout, etc.) on the deployed server.
    _log.info("[notion] %s", msg)


load_dotenv()
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_PAGE_API = os.getenv("NOTION_PAGE_API")
NOTION_PAGE_CONTENT_API = os.getenv("NOTION_PAGE_CONTENT_API")
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


async def _get_json(url: str) -> dict:
    response = await _client().get(url, headers=_headers())
    _raise_with_body(response)
    return response.json()


async def _post_json(url: str, payload: dict) -> dict:
    response = await _client().post(url, headers=_headers(), json=payload)
    _raise_with_body(response)
    return response.json()


async def _patch_json(url: str, payload: dict) -> dict:
    response = await _client().patch(url, headers=_headers(), json=payload)
    _raise_with_body(response)
    return response.json()


async def _delete_json(url: str) -> dict:
    response = await _client().delete(url, headers=_headers())
    _raise_with_body(response)
    return response.json()


async def get_page_contents(notion_page_id=None, use_default=True) -> NotionPageModel:
    page_id = notion_page_id or NOTION_MAIN_PAGE_ID
    if page_id is None or not use_default:
        id_not_specified = NotionPageIdNotSpecified(
            "Notion id is not found or is empty", 422, sys._getframe().f_code.co_name
        )
        notion_repository.save_notion_error_log(id_not_specified)
        raise id_not_specified

    url_main = NOTION_PAGE_API + page_id
    url_page = NOTION_PAGE_CONTENT_API.replace("_NOTION_PAGE_ID_", page_id)

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
            full_text += f"📃<b>{content.title}</b>\n"
        elif isinstance(content, NotionParagraphs):
            for text in content.texts:
                annotation = text.annotation
                if annotation.bold:
                    full_text += f"<b>{text.plain_text}</b>"
                elif annotation.italic:
                    full_text += f"<i>{text.plain_text}</i>"
                elif annotation.underline:
                    full_text += f"<u>{text.plain_text}</u>"
                elif annotation.strike:
                    full_text += f"<s>{text.plain_text}</s>"
                else:
                    full_text += f"{text.plain_text}"
            full_text += "\n"

    return full_text


async def create_page(parent_page_id: str, title: str, body: str) -> str:
    """Create a Notion page under `parent_page_id` with the given title and body. Returns the new page id."""
    blocks = _text_to_paragraph_blocks(body)
    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "properties": {
            "title": {
                "title": [{"type": "text", "text": {"content": title[:NOTION_RICH_TEXT_LIMIT]}}]
            }
        },
        "children": blocks[:NOTION_BLOCKS_PER_REQUEST],
    }
    response = await _post_json(f"{NOTION_API_BASE}/pages", payload)
    page_id = response["id"]

    remaining = blocks[NOTION_BLOCKS_PER_REQUEST:]
    while remaining:
        batch = remaining[:NOTION_BLOCKS_PER_REQUEST]
        remaining = remaining[NOTION_BLOCKS_PER_REQUEST:]
        await _patch_json(f"{NOTION_API_BASE}/blocks/{page_id}/children", {"children": batch})

    return page_id


async def move_page(page_id: str, new_parent_page_id: str) -> None:
    payload = {"parent": {"type": "page_id", "page_id": new_parent_page_id}}
    await _patch_json(f"{NOTION_API_BASE}/pages/{page_id}", payload)


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
        url = NOTION_PAGE_CONTENT_API.replace("_NOTION_PAGE_ID_", parent_page_id)
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
        data = await _post_json(f"{NOTION_API_BASE}/databases/{database_id}/query", payload)
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
        await _patch_json(f"{NOTION_API_BASE}/blocks/{page_id}/children", {"children": batch})

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
        url = NOTION_PAGE_CONTENT_API.replace("_NOTION_PAGE_ID_", page_id)
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
