from typing import List

from os import getenv
from aiogram.types import ReplyKeyboardMarkup, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

from bot.model.bot_models import Channel, User, UserPosts
from notion.model.notion import *

load_dotenv()

def get_admin_keyboards() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()

    builder.button(text=getenv("NOTION_MAIN_WORKSPACE_BUTTON_TEXT", "NONE"))
    builder.button(text="Ideas 💡")
    builder.button(text="Reminders ⏰")
    builder.button(text="Settings ⚙️")
    builder.button(text="Sync 🔄")

    builder.adjust(3, 2)

    return builder.as_markup(resize_keyboard=True)

def get_inline_message_keyboards(user: User) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    data = (str(user.id) + "_:_" + str(user.chat_id))

    builder.button(text="See this message, delete", callback_data=data)

    return builder.as_markup(resize_keyboard=True)

def get_page_callback_queries(pages: List[NotionChildPage], add_back_button = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    counter = 0
    for page in pages:
        builder.button(text=page.title, callback_data="_NOTION_PAGE_ID_" + page.id)
        counter = counter + 1
        if counter == 15:
            break
    if add_back_button:
        builder.button(text=getenv("NOTION_BACK_BUTTON_TEXT", "NONE"), callback_data="CALLBACK_STEP_BACK_TO_MAIN")

    builder.adjust(3, repeat=True)
    return builder.as_markup(resize_keyboard=True)

def get_notion_workspace_page_buttons() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()

    builder.button(text="Channels 📺")
    builder.button(text="Poems 📜")
    builder.button(text="User quotes 🖊")
    builder.button(text=getenv("NOTION_BACK_BUTTON_TEXT", "NONE"))

    builder.adjust(3, 1)

    return builder.as_markup(resize_keyboard=True)

def get_forward_message_cbq(forwarded_message_id: str, post: UserPosts) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if post.is_title_by_gpt:
        builder.button(text="Regenerate by gpt", callback_data=f'REGENERATE_BY_GPT_{forwarded_message_id}')
    else:
        builder.button(text="Ask from gpt", callback_data=f'ASK_FROM_GPT_{forwarded_message_id}')

    if post.is_title_by_claude:
        builder.button(text="Regenerate by claude", callback_data=f'REGENERATE_BY_CLAUDE_{forwarded_message_id}')
    else:
        builder.button(text="Ask from claude", callback_data=f'ASK_FROM_CLAUDE_{forwarded_message_id}')

    builder.button(text="Title by myself", callback_data=f'TITLE_BY_ME_{forwarded_message_id}')

    if post.is_title_by_claude:
        builder.button(text="Save with current claude title", callback_data=f'SAVE_WITH_CLAUDE_TITLE_{forwarded_message_id}')
    if post.is_title_by_gpt:
        builder.button(text="Save with current gpt title", callback_data=f'SAVE_WITH_GPT_TITLE_{forwarded_message_id}')
    builder.button(text="Save with my title", callback_data=f'SAVE_WITH_MY_TITLE_{forwarded_message_id}')
    builder.adjust(3, repeat=True)

    return builder.as_markup(resize_keyboard=True)


CHANNELS_PER_PAGE = 8


def paginate_channels(channels: List[Channel], page: int) -> tuple[List[Channel], int, int]:
    """Return (channels_for_this_page, clamped_page, total_pages).

    Centralized so the keyboard and the message header always agree on the
    page count and the slice."""
    total = max(1, (len(channels) + CHANNELS_PER_PAGE - 1) // CHANNELS_PER_PAGE)
    page = max(0, min(page, total - 1))
    start = page * CHANNELS_PER_PAGE
    return channels[start:start + CHANNELS_PER_PAGE], page, total


def get_channels_list_keyboard(channels: List[Channel], page: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    page_channels, page, total_pages = paginate_channels(channels, page)
    base_index = page * CHANNELS_PER_PAGE
    for offset, channel in enumerate(page_channels, start=1):
        suffix = f"@{channel.username}" if channel.username else channel.external_id
        builder.button(
            text=f"{base_index + offset}. {channel.name} ({suffix})",
            callback_data=f"CH_VIEW_{channel.id}_P{page}",
        )

    nav: list[tuple[str, str]] = []
    if page > 0:
        nav.append(("⬅️ Prev", f"CH_LIST_PAGE_{page - 1}"))
    if total_pages > 1:
        nav.append((f"· {page + 1}/{total_pages} ·", "CH_LIST_NOOP"))
    if page < total_pages - 1:
        nav.append(("Next ➡️", f"CH_LIST_PAGE_{page + 1}"))
    for text, data in nav:
        builder.button(text=text, callback_data=data)

    # One channel per row, then nav buttons in a single row at the bottom.
    if nav:
        builder.adjust(*([1] * len(page_channels)), len(nav))
    else:
        builder.adjust(1, repeat=True)
    return builder.as_markup()


def get_channel_detail_keyboard(channel: Channel, from_page: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 View posts", callback_data=f"CH_POSTS_{channel.id}")
    builder.button(text="✏️ Rename", callback_data=f"CH_RENAME_{channel.id}")
    builder.button(text="🔖 Edit username", callback_data=f"CH_USERNAME_{channel.id}")
    builder.button(text="🔀 Merge into…", callback_data=f"CH_MERGE_{channel.id}")
    builder.button(text="🗑 Delete", callback_data=f"CH_DELETE_{channel.id}")
    builder.button(text="⬅️ Back to channels", callback_data=f"CH_LIST_PAGE_{from_page}")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


def get_merge_target_keyboard(source: Channel, channels: List[Channel]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for channel in channels:
        if channel.id == source.id:
            continue
        suffix = f"@{channel.username}" if channel.username else channel.external_id
        builder.button(
            text=f"{channel.name} ({suffix})",
            callback_data=f"CH_MERGE_GO_{source.id}_{channel.id}",
        )
    builder.button(text="⬅️ Cancel", callback_data=f"CH_VIEW_{source.id}")
    builder.adjust(1, repeat=True)
    return builder.as_markup()


def get_delete_confirm_keyboard(channel: Channel) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Yes, delete", callback_data=f"CH_DELETE_GO_{channel.id}")
    builder.button(text="⬅️ Cancel", callback_data=f"CH_VIEW_{channel.id}")
    builder.adjust(2)
    return builder.as_markup()


def get_channel_posts_keyboard(channel: Channel, posts: List[UserPosts]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, post in enumerate(posts, start=1):
        builder.button(text=f"{index}. {_post_label(post)}", callback_data=f"POST_VIEW_{post.id}")
    builder.button(text="⬅️ Back to channel", callback_data=f"CH_VIEW_{channel.id}")
    builder.adjust(1, repeat=True)
    return builder.as_markup()


def get_post_detail_keyboard(post: UserPosts) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Edit title", callback_data=f"POST_TITLE_{post.id}")
    builder.button(text="🔀 Move to…", callback_data=f"POST_MOVE_{post.id}")
    builder.button(text="🗑 Delete", callback_data=f"POST_DELETE_{post.id}")
    if post.channel_id is not None:
        builder.button(text="⬅️ Back", callback_data=f"CH_POSTS_{post.channel_id}")
    builder.adjust(2, 1, 1)
    return builder.as_markup()


def get_post_move_keyboard(post: UserPosts, channels: List[Channel]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for channel in channels:
        if channel.id == post.channel_id:
            continue
        suffix = f"@{channel.username}" if channel.username else channel.external_id
        builder.button(
            text=f"{channel.name} ({suffix})",
            callback_data=f"POST_MOVE_GO_{post.id}_{channel.id}",
        )
    builder.button(text="⬅️ Cancel", callback_data=f"POST_VIEW_{post.id}")
    builder.adjust(1, repeat=True)
    return builder.as_markup()


def get_post_delete_confirm_keyboard(post: UserPosts) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Yes, delete", callback_data=f"POST_DELETE_GO_{post.id}")
    builder.button(text="⬅️ Cancel", callback_data=f"POST_VIEW_{post.id}")
    builder.adjust(2)
    return builder.as_markup()


SYNC_INTERVAL_PRESETS = [(0, "Off"), (30, "30m"), (60, "1h"), (360, "6h"), (720, "12h"), (1440, "24h")]


def get_settings_keyboard(user: User) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    gpt_label = "🤖 GPT ✓" if user.auto_title_ai == "gpt" else "🤖 GPT"
    claude_label = "🧠 Claude ✓" if user.auto_title_ai == "claude" else "🧠 Claude"
    builder.button(text=gpt_label, callback_data="SET_AI_gpt")
    builder.button(text=claude_label, callback_data="SET_AI_claude")

    save_label = "💾 Auto-save: ON" if user.auto_save else "💾 Auto-save: OFF"
    builder.button(text=save_label, callback_data="SET_AUTOSAVE_TOGGLE")

    for minutes, label in SYNC_INTERVAL_PRESETS:
        marker = " ✓" if user.auto_sync_interval_minutes == minutes else ""
        builder.button(text=f"{label}{marker}", callback_data=f"SET_SYNC_{minutes}")

    builder.adjust(2, 1, 3, 3)
    return builder.as_markup()


def _post_label(post: UserPosts) -> str:
    title = (
        post.saved_title
        or post.custom_title
        or post.gpt_title
        or post.claude_title
        or (post.post[:40].strip() if post.post else "(no title)")
    )
    if len(title) > 50:
        title = title[:47] + "…"
    return title
