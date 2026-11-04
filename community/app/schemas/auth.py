from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from app.schemas.common import InputModel


class Department(BaseModel):
    department_id: str
    name: str | None = None


class Principal(BaseModel):
    user_id: str
    tenant_id: str
    display_name: str
    roles: list[Literal["member", "admin"]] = Field(default_factory=lambda: ["member"])
    identity_source: Literal["development", "feishu"]
    email: str | None = None
    departments: list[Department] = Field(default_factory=list)
    profile_status: Literal["complete", "partial", "unavailable"] = "unavailable"


class SessionRecord(BaseModel):
    principal: Principal
    expires_at: datetime


class LoginResult(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
    user: Principal


class FeishuLoginInput(InputModel):
    code: SecretStr = Field(min_length=1, max_length=2048)


class LogoutResult(BaseModel):
    logged_out: bool = True
