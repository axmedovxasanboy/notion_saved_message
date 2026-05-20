from typing import List, Optional

from bot.model.bot_models import Channel, Favorite, FavoriteType, UserPosts
from container import services


def is_channel_favorite(user_id: int, channel_id: int) -> bool:
    return services.db.get_favorite(user_id, FavoriteType.CHANNEL, channel_id) is not None


def is_post_favorite(user_id: int, post_id: int) -> bool:
    return services.db.get_favorite(user_id, FavoriteType.POST, post_id) is not None


def toggle_channel_favorite(user_id: int, channel_id: int) -> bool:
    """Toggle a channel favorite. Returns True if it's now favorited, False if removed."""
    if is_channel_favorite(user_id, channel_id):
        services.db.remove_favorite(user_id, FavoriteType.CHANNEL, channel_id)
        return False
    services.db.add_favorite(user_id, FavoriteType.CHANNEL, channel_id)
    return True


def toggle_post_favorite(user_id: int, post_id: int) -> bool:
    if is_post_favorite(user_id, post_id):
        services.db.remove_favorite(user_id, FavoriteType.POST, post_id)
        return False
    services.db.add_favorite(user_id, FavoriteType.POST, post_id)
    return True


def list_favorite_channels(user_id: int) -> List[Channel]:
    return services.db.list_favorite_channels(user_id)


def list_favorite_posts(user_id: int) -> List[UserPosts]:
    return services.db.list_favorite_posts(user_id)


def remove_channel_favorites(channel_id: int) -> None:
    services.db.cleanup_favorite_channel(channel_id)


def remove_post_favorites(post_id: int) -> None:
    services.db.cleanup_favorite_post(post_id)
