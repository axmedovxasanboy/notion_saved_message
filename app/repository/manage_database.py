import logging
from pathlib import Path
from sqlalchemy import Engine

from bot.model.bot_models import (
    Channel, Favorite, FavoriteType, PostDestination, PostTag, User, UserPosts,
)
# Imported for its side effect: registers the NotionLogs table on SQLModel's
# metadata so create_all() below creates it.
from notion.model.notion import NotionLogs  # noqa: F401
from dotenv import load_dotenv
from sqlmodel import SQLModel, create_engine, Session, select
import os
import uuid

load_dotenv()

_log = logging.getLogger(__name__)

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
        # Migrations are NOT best-effort: a missing column crashes the first
        # query that touches it anyway, only later and with a far more
        # confusing traceback. Fail here, loudly, at startup.
        self._apply_lightweight_migrations()
        try:
            self._backfill_channels()
        except Exception:
            # Backfill only regroups legacy rows — log it, don't block startup.
            _log.exception("Channel backfill failed; continuing without it")

    def _apply_lightweight_migrations(self) -> None:
        """Add columns that newer model versions introduced to already-existing tables.
        SQLModel.metadata.create_all() only creates *missing* tables; it never ALTERs."""
        from sqlalchemy import inspect, text
        inspector = inspect(self._engine)
        if "user" in inspector.get_table_names():
            columns = inspector.get_columns("user")
            existing = {col["name"] for col in columns}
            if "current_channel_page" not in existing:
                with self._engine.begin() as conn:
                    conn.execute(text(
                        'ALTER TABLE "user" ADD COLUMN current_channel_page INTEGER '
                        'NOT NULL DEFAULT 0'
                    ))
            if "posts_sort_order" not in existing:
                with self._engine.begin() as conn:
                    conn.execute(text(
                        'ALTER TABLE "user" ADD COLUMN posts_sort_order VARCHAR '
                        "NOT NULL DEFAULT 'desc'"
                    ))
            username = next((c for c in columns if c["name"] == "username"), None)
            if username is not None and not username.get("nullable", True):
                self._rebuild_user_table_nullable_username()

    def _rebuild_user_table_nullable_username(self) -> None:
        """Relax the legacy NOT NULL on user.username (Telegram accounts don't
        have to set a @username; inserting None crashed /start on old DBs).

        SQLite can't drop NOT NULL via ALTER, so this follows the documented
        rebuild recipe: create the new-shape table under a temp name, copy the
        rows, drop the old table, rename the new one in. Renaming the OLD
        table first would be wrong — modern SQLite rewrites other tables'
        foreign-key clauses to follow a rename, which would leave them
        pointing at the temp name. FK enforcement is off (SQLAlchemy's SQLite
        driver never enables the pragma), so the drop is safe."""
        from sqlalchemy import MetaData, inspect, text
        _log.info("Migrating: rebuilding 'user' table to make username nullable")
        tmp_name = "user_migration_new"
        tmp_metadata = MetaData()
        tmp_table = User.__table__.to_metadata(tmp_metadata, name=tmp_name)
        with self._engine.begin() as conn:
            conn.execute(text(f'DROP TABLE IF EXISTS "{tmp_name}"'))
            tmp_table.create(conn)
            old_cols = {c["name"] for c in inspect(conn).get_columns("user")}
            common = [c.name for c in User.__table__.columns if c.name in old_cols]
            col_list = ", ".join(f'"{c}"' for c in common)
            conn.execute(text(
                f'INSERT INTO "{tmp_name}" ({col_list}) SELECT {col_list} FROM "user"'
            ))
            conn.execute(text('DROP TABLE "user"'))
            conn.execute(text(f'ALTER TABLE "{tmp_name}" RENAME TO "user"'))

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

    def get_user_by_chat_id(self, chat_id: str) -> User | None:
        # Deliberately does NOT eager-load user.posts: this runs on nearly
        # every update (often more than once), and loading the full posts
        # table each time gets slow fast. Posts are queried directly where
        # needed (get_post_by_message_id, list_posts_for_channel, ...).
        with Session(self._engine) as session:
            return session.exec(
                select(User).where(User.chat_id == chat_id)
            ).first()

    def get_post_by_message_id(self, user_id: int, message_id: str) -> "UserPosts | None":
        with Session(self._engine) as session:
            return session.exec(
                select(UserPosts).where(
                    UserPosts.user_id == user_id,
                    UserPosts.message_id == str(message_id),
                )
            ).first()

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

    def list_posts_for_channel(self, channel_id: int, sort_order: str = "desc") -> list:
        # Order by the original post date (taken from forward_origin.date), not by
        # creation/forward time. Posts with no original_post_date (older synced rows)
        # are pushed to the end regardless of direction so they never crowd the top.
        # `id` is the tiebreaker so the order is deterministic across requests.
        with Session(self._engine) as session:
            stmt = select(UserPosts).where(UserPosts.channel_id == channel_id)
            if sort_order == "asc":
                stmt = stmt.order_by(
                    UserPosts.original_post_date.is_(None),
                    UserPosts.original_post_date.asc(),
                    UserPosts.id.asc(),
                )
            else:
                stmt = stmt.order_by(
                    UserPosts.original_post_date.is_(None),
                    UserPosts.original_post_date.desc(),
                    UserPosts.id.desc(),
                )
            return list(session.exec(stmt).all())

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

    # TAGS

    def get_tags_for_post(self, post_id: int) -> list[str]:
        with Session(self._engine) as session:
            rows = session.exec(
                select(PostTag.tag)
                .where(PostTag.post_id == post_id)
                .order_by(PostTag.tag)
            ).all()
            return [r if isinstance(r, str) else r[0] for r in rows]

    def get_tags_for_posts(self, post_ids: list[int]) -> dict[int, list[str]]:
        """Return {post_id: [tag, ...]} for the given ids in ONE query.
        Ids with no tags are filled with an empty list so callers don't
        need to check membership."""
        if not post_ids:
            return {}
        with Session(self._engine) as session:
            rows = session.execute(
                select(PostTag.post_id, PostTag.tag)
                .where(PostTag.post_id.in_(post_ids))
                .order_by(PostTag.post_id, PostTag.tag)
            ).all()
        out: dict[int, list[str]] = {pid: [] for pid in post_ids}
        for post_id, tag in rows:
            out.setdefault(int(post_id), []).append(tag)
        return out

    def set_tags_for_post(self, post_id: int, tags: list[str]) -> list[str]:
        """Replace a post's tag set wholesale. Returns the stored tags, sorted.

        Diffs against the existing rows instead of delete-all-then-insert so
        `created_at` survives on tags that were already there."""
        desired = sorted(set(tags))
        with Session(self._engine) as session:
            existing_rows = list(session.exec(
                select(PostTag).where(PostTag.post_id == post_id)
            ).all())
            existing = {row.tag: row for row in existing_rows}

            for tag, row in existing.items():
                if tag not in desired:
                    session.delete(row)
            for tag in desired:
                if tag not in existing:
                    session.add(PostTag(post_id=post_id, tag=tag))
            session.commit()
        return desired

    def list_tags_with_counts(self) -> list[tuple[str, int]]:
        """Every tag in use, with how many posts carry it.
        Ordered by count (desc) then name (asc) so the busiest tags lead."""
        from sqlalchemy import func
        with Session(self._engine) as session:
            rows = session.execute(
                select(PostTag.tag, func.count(PostTag.post_id))
                .group_by(PostTag.tag)
                .order_by(func.count(PostTag.post_id).desc(), PostTag.tag.asc())
            ).all()
        return [(tag, int(count)) for tag, count in rows]

    def list_posts_for_tag(self, tag: str, sort_order: str = "desc") -> list:
        # Same ordering contract as list_posts_for_channel: by original post
        # date, undated rows last in both directions, `id` as the tiebreaker.
        with Session(self._engine) as session:
            stmt = (
                select(UserPosts)
                .join(PostTag, PostTag.post_id == UserPosts.id)
                .where(PostTag.tag == tag)
            )
            if sort_order == "asc":
                stmt = stmt.order_by(
                    UserPosts.original_post_date.is_(None),
                    UserPosts.original_post_date.asc(),
                    UserPosts.id.asc(),
                )
            else:
                stmt = stmt.order_by(
                    UserPosts.original_post_date.is_(None),
                    UserPosts.original_post_date.desc(),
                    UserPosts.id.desc(),
                )
            return list(session.exec(stmt).all())

    def cleanup_post_tags(self, post_id: int) -> None:
        """Drop every tag row pointing at a post that no longer exists."""
        with Session(self._engine) as session:
            rows = session.exec(
                select(PostTag).where(PostTag.post_id == post_id)
            ).all()
            for row in rows:
                session.delete(row)
            session.commit()










