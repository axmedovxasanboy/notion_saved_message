from container import services
from exceptions.notion_exceptions import NotionPageIdNotSpecified
from notion.model.notion import NotionLogs
from sqlmodel import SQLModel, create_engine, Session, select



def save_notion_error_log(notion_exception: NotionPageIdNotSpecified):
    log = NotionLogs(function_name=notion_exception.function_name, error=notion_exception.__str__())

    with Session(services.db.get_engine()) as session:
        session.add(log)
        session.commit()
        session.refresh(log)

