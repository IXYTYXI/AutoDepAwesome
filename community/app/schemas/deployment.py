from typing import Literal

from pydantic import BaseModel


class DeploymentFailure(BaseModel):
    code: str
    message: str
    field: str = ""
    suggestion: str = ""


class DeploymentJob(BaseModel):
    id: str
    appId: str | None
    status: Literal[
        "queued", "running", "succeeded", "failed", "recovery_required", "cancelled"
    ]
    phase: str
    terminal: bool
    repository: str
    subdomain: str
    commit: str | None
    updatedAt: float
    url: str | None
    error: DeploymentFailure | None


class DeploymentOptions(BaseModel):
    executionEnabled: bool
    baseDomain: str
    gitHosts: list[str]
    maxAppMemoryMiB: int
    message: str
