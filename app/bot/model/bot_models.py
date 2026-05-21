from datetime import datetime
from enum import Enum
from typing import List, Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel

class NotionPageType(Enum):
    MAIN = 1
    CHANNEL = 2
    POEM = 3
    USER = 4
    IDEAS = 5
    REMINDERS = 6

class BotSteps(Enum):
    MAIN = 1
    WORKSPACE = 2
    CHANNEL = 21
    POEM = 22
    USER_QUOTES = 23
    IDEA = 3
    REMINDERS = 4
    SETTINGS = 5
    CALLBACK_MAIN = 100
    CALLBACK_CHANNELS = 101
    CALLBACK_POEMS = 102
    CALLBACK_USER_QUOTES = 103

class PostDestination(str, Enum):
    CHANNEL = "channel"
    POEM = "poem"
    USER_QUOTE = "user_quote"


class UserPosts(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    post: str = Field(nullable=False)
    message_id: str = Field(nullable=False)
    is_title_by_gpt: bool = Field(default=False, nullable=False)
    is_title_by_claude: bool = Field(default=False, nullable=False)
    gpt_title: Optional[str] = Field(default=None)
    claude_title: Optional[str] = Field(default=None)
    custom_title: Optional[str] = Field(default=None)
    saved_title: Optional[str] = Field(default=None)
    source_channel_name: Optional[str] = Field(default=None)
    source_channel_username: Optional[str] = Field(default=None)
    source_user_name: Optional[str] = Field(default=None)
    source_user_username: Optional[str] = Field(default=None)
    destination: PostDestination = Field(default=PostDestination.USER_QUOTE, nullable=False)
    saved_notion_page_id: Optional[str] = Field(default=None)
    # UTC datetime taken from Telegram's forward_origin.date when available
    # (the time the *original* post was published, not when the admin forwarded it).
    # Drives the "newest first" Posted At property in Notion databases.
    original_post_date: Optional[datetime] = Field(default=None)

    user_id: int = Field(foreign_key="user.id")
    channel_id: Optional[int] = Field(default=None, foreign_key="channel.id")

    user: "User" = Relationship(back_populates="posts")
    channel: Optional["Channel"] = Relationship(back_populates="posts")


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True)
    first_name: str = Field(nullable=False)
    last_name: Optional[str]
    chat_id: str = Field(nullable=False)
    step: BotSteps = Field(default=BotSteps.MAIN, nullable=False)
    callback_step: BotSteps = Field(default=BotSteps.CALLBACK_MAIN, nullable=False)
    awaiting_title_for_message_id: Optional[str] = Field(default=None)
    awaiting_action: Optional[str] = Field(default=None)
    auto_title_ai: str = Field(default="gpt", nullable=False)
    auto_save: bool = Field(default=False, nullable=False)
    auto_sync_interval_minutes: int = Field(default=0, nullable=False)
    # Page index (0-based) the admin is currently viewing in the paginated channels
    # list. Persisted so that navigating channel → posts → post → back returns to
    # the original page instead of resetting to page 1.
    current_channel_page: int = Field(default=0, nullable=False)
    # "desc" (newest first) or "asc" (oldest first) — sort applied to the per-channel
    # posts list, keyed on UserPosts.original_post_date.
    posts_sort_order: str = Field(default="desc", nullable=False)

    posts: List["UserPosts"] = Relationship(back_populates="user")


class FavoriteType(str, Enum):
    CHANNEL = "channel"
    POST = "post"


class Favorite(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("user_id", "target_type", "target_id", name="uq_favorite_user_target"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", nullable=False)
    target_type: FavoriteType = Field(nullable=False)
    target_id: int = Field(nullable=False)
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)


class Channel(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(nullable=False)
    username: Optional[str] = Field(default=None)
    external_id: str = Field(nullable=False, unique=True)
    # The page id of this channel's row inside the top-level "Channels Index"
    # Notion database. Same field name kept so existing UI links keep working.
    notion_page_id: Optional[str] = Field(default=None)
    # The id of the per-channel "Posts" database that lives inside this
    # channel's row page. Created lazily on first save.
    notion_posts_db_id: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.now)

    posts: List["UserPosts"] = Relationship(back_populates="channel")
