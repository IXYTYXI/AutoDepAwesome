from datetime import datetime
from pydantic import BaseModel


class FAQItem(BaseModel):
    id: str
    question: str
    answer: str


class FAQSnapshot(BaseModel):
    items: list[FAQItem]
    updated_at: datetime
    stale: bool = False
