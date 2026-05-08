import datetime
import uuid
from typing import List, Optional
from enum import Enum
from sqlmodel import SQLModel, Field
from dotenv import load_dotenv

load_dotenv()

class NotionLogs(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    function_name: str = Field(default=None, nullable=False)
    error: str = Field(default=None, nullable=False)
    occurred_at: datetime.datetime = Field(default_factory=datetime.datetime.now, nullable=False)

class NotionPageCreator:
    # wrap following field to "parent" json object
    page_id: str = ""
    # wrap following field to "properties" and "title" list item
    type: str = "text"
    # wrap following field to "text" json object
    title: str
    content: str

class NotionTypes(Enum):
    TITLE = 1
    TEXT = 2
    CHILD_PAGE = 3


class NotionObjects(Enum):
    PAGE = 1
    BLOCK = 2


class FavouritePage(SQLModel, table=True):
    id: str | None = Field(default=None, primary_key=True)
    title: str | None = Field(default=None)


class NotionAnnotations:
    bold: bool
    italic: bool
    underline: bool
    strike: bool

    def __init__(self, bold: bool, italic: bool, underline: bool, strike: bool) -> None:
        self.bold = bold
        self.italic = italic
        self.underline = underline
        self.strike = strike


class NotionChildPage:
    id: str
    title: str
    order: int

    def __init__(self, id: str, title: str, order) -> None:
        self.id = id
        self.title = title
        self.order = order

class NotionText:
    type: NotionTypes
    plain_text: str
    href: Optional[str]
    annotation: NotionAnnotations

    def __init__(self, type: NotionTypes, text: str, href, annotation) -> None:
        self.type = type
        self.plain_text = text
        self.href = href
        self.annotation = annotation

class NotionParagraphs:
    texts: List[NotionText]
    order: int

    def __init__(self, texts: List[NotionText], order: int) -> None:
        self.texts = texts
        self.order = order


class NotionPageModel:
    id: str
    title: str
    object_type: NotionObjects
    page: Optional[List[NotionChildPage]]
    paragraphs: Optional[List[NotionParagraphs]]

    def __init__(self, id: str, title: str, object_type: NotionObjects, page=None, paragraphs=None) -> None:
        self.id = id
        self.title = title
        self.object_type = object_type
        self.page = page
        self.paragraphs = paragraphs

    def get_notion_page_for_creating(self):
        pass
