from datetime import UTC, datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request

from app.core.error_codes import ErrorCode

T = TypeVar("T")


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ErrorDetail(BaseModel):
    field: str
    reason: str


class ErrorInfo(BaseModel):
    details: list[ErrorDetail] = Field(default_factory=list)


class Envelope(BaseModel, Generic[T]):
    code: str = "OK"
    message: str = "成功"
    data: T | None = None
    error: ErrorInfo | None = None
    request_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ErrorEnvelope(Envelope[None]):
    code: ErrorCode
    error: ErrorInfo = Field(...)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


def success(request: Request, data: T, message: str = "成功") -> Envelope[T]:
    return Envelope(data=data, message=message, request_id=request.state.request_id)
