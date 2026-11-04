from datetime import datetime
from pydantic import BaseModel


class Banner(BaseModel):
    id: str
    title: str = ''
    summary: str = ''
    image: str | None = None
    url: str | None = None


class BannerSnapshot(BaseModel):
    item: Banner | None = None
    updated_at: datetime
    stale: bool = False
