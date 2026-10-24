from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: object | None = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class OkResponse(BaseModel):
    ok: bool = True


__all__ = ["ORM", "Page", "ErrorResponse", "OkResponse", "datetime", "UUID"]
