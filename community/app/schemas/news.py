from datetime import datetime
from pydantic import BaseModel, Field


class NewsItem(BaseModel):
    id: str
    title: str
    summary: str = ''
    source: str = ''
    category: str = '其他'
    published_at: datetime | None = None
    discovered_at: datetime | None = None
    url: str | None = None
    aihot_url: str | None = None
    image: str | None = None
    featured: bool = False
    weight: float = 0
    attribution: dict = Field(default_factory=dict)


class NewsDirectory(BaseModel):
    items: list[NewsItem]
    updated_at: datetime
    stale: bool = False
