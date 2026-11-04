from typing import Literal
from uuid import UUID
from pydantic import Field
from app.schemas.common import InputModel


class ChatInput(InputModel):
    session_id: UUID
    request_id: UUID
    message: str = Field(min_length=1, max_length=2000)
    page: str = Field(default='/', max_length=160, pattern=r'^/[a-zA-Z0-9_./-]*$')


class TicketDraft(InputModel):
    title: str = Field(min_length=1, max_length=80)
    category: Literal['功能建议', '网站错误', '使用问题']
    description: str = Field(min_length=1, max_length=3000)


class TicketDecision(InputModel):
    session_id: UUID
    confirmed: bool
