from datetime import datetime
from pydantic import BaseModel, Field


class CaseContact(BaseModel):
    name: str
    chat_url: str


class Case(BaseModel):
    id: str
    title: str
    summary: str = ''
    team: str = ''
    author: str = ''
    tags: list[str] = Field(default_factory=list)
    product_type: str = ''
    source: str = ''
    number: str = ''
    access: str
    url: str | None = None
    contact_url: str | None = None
    ai_officers: list[CaseContact] = Field(default_factory=list)
    image: str | None = None


class CaseDirectory(BaseModel):
    items: list[Case]
    updated_at: datetime
    stale: bool = False
