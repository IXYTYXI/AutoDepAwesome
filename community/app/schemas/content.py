from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Case(BaseModel):
    id: str
    title: str
    category: str
    source: str
    summary: str
    body: str = ""
    image: str = ""
    url: str = ""
    type: Literal["featured", "cover"] = "cover"


class News(BaseModel):
    id: str
    title: str
    source: str
    summary: str
    category: str
    url: str = ""


class Banner(BaseModel):
    title: str
    summary: str
    kicker: str
    image: str = ""
    url: str = ""


class ContentSnapshot(BaseModel):
    version: str = Field(min_length=1, max_length=64)
    synced_at: datetime
    cases: list[Case] = Field(default_factory=list)
    news: list[News] = Field(default_factory=list)
    banner: Banner | None = None

    @model_validator(mode="after")
    def validate_snapshot(self):
        if self.synced_at.tzinfo is None:
            raise ValueError("synced_at 必须包含时区")
        for records in (self.cases, self.news):
            if len({item.id for item in records}) != len(records):
                raise ValueError("记录 ID 不可重复")
        return self
