import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_RFC1123_RE = re.compile(r"^[a-z0-9]([a-z0-9\-.]*[a-z0-9])?$")


class HealthResponse(BaseModel):
    status: str
    kubernetes_connected: bool
    message: str
    timestamp: datetime


class RestoreSource(BaseModel):
    path: str
    size_bytes: int
    modified_at: datetime
    extension: str


class RestoreJobRequest(BaseModel):
    namespace: str = Field(min_length=1)
    environment: Literal["dev", "prod"] = "prod"
    source_path: str = Field(min_length=1)
    database_secret_name: str = Field(min_length=1)
    target_database: str = Field(min_length=1)
    job_name_prefix: str = Field(default="postgres-restore", min_length=3, max_length=32)

    @field_validator("database_secret_name", "namespace")
    @classmethod
    def validate_k8s_name(cls, value: str) -> str:
        if not _RFC1123_RE.match(value):
            raise ValueError(f"'{value}' is not a valid Kubernetes name — use lowercase letters, numbers, and hyphens only (no underscores)")
        return value

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, value: str) -> str:
        normalized = value.strip().lstrip("/")
        if not normalized or ".." in normalized.split("/"):
            raise ValueError("source_path must point to a file under the configured backup directory")
        return normalized


class RestoreValidationResponse(BaseModel):
    valid: bool
    errors: list[str]
    warnings: list[str]
    resolved_source_path: str | None = None


class RestoreJobResponse(BaseModel):
    status: str
    job_name: str
    namespace: str
    source_path: str
    created_at: datetime


class JobStatusResponse(BaseModel):
    job_name: str
    namespace: str
    active: int = 0
    succeeded: int = 0
    failed: int = 0
    conditions: list[str] = Field(default_factory=list)
