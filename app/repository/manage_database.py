from pathlib import Path
from sqlalchemy import Engine
from sqlalchemy.orm import selectinload

from bot.model.bot_models import Channel, PostDestination, User, UserPosts
from exceptions.notion_exceptions import *
from notion.model.notion import *
from dotenv import load_dotenv
from sqlmodel import SQLModel, create_engine, Session, select
import os
import uuid

load_dotenv()

# Project root resolved from this file's location, not from CWD —
# stable whether the bot is launched via `python app/main.py`, a service
# unit on a server, a Docker container, or any other entry point.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve_sqlite_url() -> str:
    """Build (and prepare) the SQLite URL using dynamic, portable paths.

    Resolution order:
      1. ``DB_URL``                — full SQLAlchemy URL, used as-is.
      2. ``DATA_DIR`` + ``DB_NAME`` — folder + filename (relative paths
         are resolved against the project root).
      3. Defaults: ``<project_root>/data/ai_agent_bot.db``.

    The parent folder is always created if missing.
    """
    explicit_url = os.getenv("DB_URL")
    if explicit_url:
        if explicit_url.startswith("sqlite:///"):
            raw_path = explicit_url[len("sqlite:///"):]
            db_path = Path(raw_path)
            if not db_path.is_absolute():
                db_path = PROJECT_ROOT / db_path
            db_path.parent.mkdir(parents=True, exist_ok=True)
            return f"sqlite:///{db_path}"
        return explicit_url

    data_dir = Path(os.getenv("DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    db_name = os.getenv("DB_NAME", "ai_agent_bot.db")
    db_path = data_dir / db_name
    return f"sqlite:///{db_path}"


class DatabaseManager:
    _engine: Engine

    def __init__(self):
        db_url = _resolve_sqlite_url()
        connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
        self._engine = create_engine(db_url, connect_args=connect_args)
        SQLModel.metadata.create_all(self._engine)
        try:
            self._backfill_channels()
        except Exception:
            # Backfill is best-effort; if the schema isn't ready yet, skip silently.
            pass

    def _backfill_channels(self) -> None:
        """Group existing channel-destined posts into Channel records (idempotent)."""
        with Session(self._engine) as session:
            unlinked = list(session.exec(
                select(UserPosts).where(
                    UserPosts.destination == PostDestination.CHANNEL,
                    UserPosts.channel_id.is_(None),
                )
            ).all())
            if not unlinked:
                return

            cache: dict[str, int] = {}
            for post in unlinked:
                username = (post.source_channel_username or "").strip().lower() or None
                name = post.source_channel_name or post.source_channel_username or "Unknown channel"
                key = f"un_{username}" if username else f"legacy_{name.lower()}"
                channel_id = cache.get(key)
                if channel_id is None:
                    existing = session.exec(
                        select(Channel).where(Channel.external_id == key)
                    ).first()
                    if existing is None:
                        new_channel = Channel(
                            name=name,
                            username=username,
                            external_id=key if username else f"gen_{uuid.uuid4().hex[:8]}",
                        )
                        session.add(new_channel)
                        session.commit()
                        session.refresh(new_channel)
                        existing = new_channel
                    channel_id = existing.id
                    cache[key] = channel_id
                post.channel_id = channel_id
                session.add(post)
            session.commit()

    def get_engine(self):
        return self._engine

    # NOTION RELATED DATABASE MANAGING

    def save_notion_error_log(self, notion_exception: NotionPageIdNotSpecified):

        log = NotionLogs(function_name = notion_exception.function_name, error=notion_exception.__str__())

        with Session(self._engine) as session:
            session.add(log)
            session.commit()
            session.refresh(log)


    def get_user_by_chat_id(self, chat_id: str) -> User | None:
        with Session(self._engine) as session:
            user = session.exec(
                select(User)
                .where(User.chat_id == chat_id)
                .options(selectinload(User.posts))
            ).first()
            if user:
                return user
            return None

    def save_or_update_post(self, user_post: UserPosts) -> UserPosts:
        with Session(self._engine) as session:
            session.add(user_post)
            session.commit()
            session.refresh(user_post)
            return user_post

    def get_post_by_id(self, post_id: int) -> "UserPosts | None":
        with Session(self._engine) as session:
            return session.exec(select(UserPosts).where(UserPosts.id == post_id)).first()

    def delete_post(self, post: "UserPosts") -> None:
        with Session(self._engine) as session:
            session.delete(session.merge(post))
            session.commit()

    # CHANNEL RELATED DATABASE MANAGING

    def get_channel_by_id(self, channel_id: int) -> "Channel | None":
        with Session(self._engine) as session:
            return session.exec(select(Channel).where(Channel.id == channel_id)).first()

    def get_channel_by_external_id(self, external_id: str) -> "Channel | None":
        with Session(self._engine) as session:
            return session.exec(select(Channel).where(Channel.external_id == external_id)).first()

    def get_channel_by_notion_id(self, notion_page_id: str) -> "Channel | None":
        with Session(self._engine) as session:
            return session.exec(select(Channel).where(Channel.notion_page_id == notion_page_id)).first()

    def get_post_by_notion_id(self, notion_page_id: str) -> "UserPosts | None":
        with Session(self._engine) as session:
            return session.exec(
                select(UserPosts).where(UserPosts.saved_notion_page_id == notion_page_id)
            ).first()

    def list_channels(self) -> list:
        with Session(self._engine) as session:
            return list(session.exec(select(Channel).order_by(Channel.name)).all())

    def save_channel(self, channel: "Channel") -> "Channel":
        with Session(self._engine) as session:
            session.add(channel)
            session.commit()
            session.refresh(channel)
            return channel

    def update_channel(self, channel: "Channel") -> "Channel":
        with Session(self._engine) as session:
            merged = session.merge(channel)
            session.commit()
            session.refresh(merged)
            return merged

    def delete_channel(self, channel: "Channel") -> None:
        with Session(self._engine) as session:
            session.delete(session.merge(channel))
            session.commit()

    def count_posts_for_channel(self, channel_id: int) -> int:
        from sqlalchemy import func
        with Session(self._engine) as session:
            result = session.exec(
                select(func.count(UserPosts.id)).where(UserPosts.channel_id == channel_id)
            ).one()
            return int(result if not isinstance(result, tuple) else result[0])

    def list_posts_for_channel(self, channel_id: int) -> list:
        with Session(self._engine) as session:
            return list(session.exec(
                select(UserPosts).where(UserPosts.channel_id == channel_id).order_by(UserPosts.id)
            ).all())

    def reassign_posts(self, from_channel_id: int, to_channel_id: int) -> int:
        with Session(self._engine) as session:
            posts = list(session.exec(
                select(UserPosts).where(UserPosts.channel_id == from_channel_id)
            ).all())
            for post in posts:
                post.channel_id = to_channel_id
                session.add(post)
            session.commit()
            return len(posts)










