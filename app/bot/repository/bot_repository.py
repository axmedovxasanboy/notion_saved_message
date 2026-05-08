from sqlmodel import Session, select

from container import services
from bot.model.bot_models import *


# ----------- USER REPOSITORY METHODS -----------

def save_user(user_data: User) -> User:
    with Session(services.db.get_engine()) as session:
        session.add(user_data)
        session.commit()
        return user_data


def update_user(user_data: User) -> User:
    with Session(services.db.get_engine()) as session:
        session.merge(user_data)
        session.commit()
        return user_data


# def get_messages():
#     with Session(services.db.get_engine()) as session:
#         statement = (select(User)
#                      .options(selectinload(User.messages)))
#         result = session.exec(statement)
#         messages = result.all()
#         return messages
#
# def save_message(message: Message):
#     with Session(services.db.get_engine()) as session:
#         user = session.exec(select(User).where(User.id == message.user_id)).first()
#
#         if not user:
#             save_user(user)
#
#         session.add(message)
#         session.commit()
#
# def delete_message_of_chat(chat_id: int):
#     with Session(services.db.get_engine()) as session:
#         statement = (select(User).where(str(chat_id) == User.chat_id).options(selectinload(User.messages)))
#         result = session.exec(statement)
#         user_msgs = result.all()
#         session.commit()
#     delete_message(user_msgs)
#
# def delete_message(messages):
#     with Session(services.db.get_engine()) as session:
#         for msg in messages:
#             session.delete(msg)
#         session.commit()
