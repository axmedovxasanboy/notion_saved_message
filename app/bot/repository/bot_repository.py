from sqlmodel import Session

from container import services
from bot.model.bot_models import User


# ----------- USER REPOSITORY METHODS -----------

def save_user(user_data: User) -> User:
    with Session(services.db.get_engine()) as session:
        session.add(user_data)
        session.commit()
        session.refresh(user_data)
        session.expunge(user_data)
        return user_data


def update_user(user_data: User) -> User:
    with Session(services.db.get_engine()) as session:
        merged = session.merge(user_data)
        session.commit()
        session.refresh(merged)
        session.expunge(merged)
        return merged
