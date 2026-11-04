from datetime import datetime
from pydantic import BaseModel, Field


class Officer(BaseModel):
    id: str
    name: str
    title: str
    team: str = ''
    position: str = ''
    tags: list[str] = Field(default_factory=list)
    bio: str = ''
    image: str | None = None
    chat_url: str | None = None


class OfficerDirectory(BaseModel):
    items: list[Officer]
    updated_at: datetime
    stale: bool = False
