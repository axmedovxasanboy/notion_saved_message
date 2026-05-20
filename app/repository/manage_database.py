from pathlib import Path
from sqlalchemy import Engine
from sqlalchemy.orm import selectinload

from bot.model.bot_models import Channel, Favorite, FavoriteType, PostDestination, User, UserPosts
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
            self._apply_lightweight_migrations()
        except Exception:
            # Migrations are best-effort; if the schema isn't ready yet, skip silently.
            pass
        try:
            self._backfill_channels()
        except Exception:
            # Backfill is best-effort; if the schema isn't ready yet, skip silently.
            pass

    def _apply_lightweight_migrations(self) -> None:
        """Add columns that newer model versions introduced to already-existing tables.
        SQLModel.metadata.create_all() only creates *missing* tables; it never ALTERs."""
        from sqlalchemy import inspect, text
        inspector = inspect(self._engine)
        if "user" in inspector.get_table_names():
            existing = {col["name"] for col in inspector.get_columns("user")}
            if "current_channel_page" not in existing:
                with self._engine.begin() as conn:
                    conn.execute(text(
                        'ALTER TABLE "user" ADD COLUMN current_channel_page INTEGER '
                        'NOT NULL DEFAULT 0'
                    ))

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
            merged = session.merge(user_post)
            session.commit()
            session.refresh(merged)
            return merged

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

    def count_posts_by_channels(self, channel_ids: list[int]) -> dict[int, int]:
        """Return {channel_id: post_count} for the given ids in ONE grouped query.
        Missing ids are filled with 0 so callers don't need to check membership."""
        if not channel_ids:
            return {}
        from sqlalchemy import func
        with Session(self._engine) as session:
            rows = session.execute(
                select(UserPosts.channel_id, func.count(UserPosts.id))
                .where(UserPosts.channel_id.in_(channel_ids))
                .group_by(UserPosts.channel_id)
            ).all()
        counts: dict[int, int] = {int(cid): int(c) for cid, c in rows if cid is not None}
        for cid in channel_ids:
            counts.setdefault(cid, 0)
        return counts

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

    # FAVORITES

    def get_favorite(self, user_id: int, target_type: FavoriteType, target_id: int) -> "Favorite | None":
        with Session(self._engine) as session:
            return session.exec(
                select(Favorite).where(
                    Favorite.user_id == user_id,
                    Favorite.target_type == target_type,
                    Favorite.target_id == target_id,
                )
            ).first()

    def add_favorite(self, user_id: int, target_type: FavoriteType, target_id: int) -> "Favorite":
        existing = self.get_favorite(user_id, target_type, target_id)
        if existing is not None:
            return existing
        with Session(self._engine) as session:
            favorite = Favorite(user_id=user_id, target_type=target_type, target_id=target_id)
            session.add(favorite)
            session.commit()
            session.refresh(favorite)
            return favorite

    def remove_favorite(self, user_id: int, target_type: FavoriteType, target_id: int) -> bool:
        with Session(self._engine) as session:
            favorite = session.exec(
                select(Favorite).where(
                    Favorite.user_id == user_id,
                    Favorite.target_type == target_type,
                    Favorite.target_id == target_id,
                )
            ).first()
            if favorite is None:
                return False
            session.delete(favorite)
            session.commit()
            return True

    def list_favorite_channels(self, user_id: int) -> list:
        with Session(self._engine) as session:
            rows = session.exec(
                select(Channel)
                .join(Favorite, Favorite.target_id == Channel.id)
                .where(
                    Favorite.user_id == user_id,
                    Favorite.target_type == FavoriteType.CHANNEL,
                )
                .order_by(Favorite.created_at.desc())
            ).all()
            return list(rows)

    def list_favorite_posts(self, user_id: int) -> list:
        with Session(self._engine) as session:
            rows = session.exec(
                select(UserPosts)
                .join(Favorite, Favorite.target_id == UserPosts.id)
                .where(
                    Favorite.user_id == user_id,
                    Favorite.target_type == FavoriteType.POST,
                )
                .order_by(Favorite.created_at.desc())
            ).all()
            return list(rows)

    def cleanup_favorite_channel(self, channel_id: int) -> None:
        """Drop any favorite rows pointing at a channel that no longer exists."""
        with Session(self._engine) as session:
            rows = session.exec(
                select(Favorite).where(
                    Favorite.target_type == FavoriteType.CHANNEL,
                    Favorite.target_id == channel_id,
                )
            ).all()
            for row in rows:
                session.delete(row)
            session.commit()

    def cleanup_favorite_post(self, post_id: int) -> None:
        with Session(self._engine) as session:
            rows = session.exec(
                select(Favorite).where(
                    Favorite.target_type == FavoriteType.POST,
                    Favorite.target_id == post_id,
                )
            ).all()
            for row in rows:
                session.delete(row)
            session.commit()










