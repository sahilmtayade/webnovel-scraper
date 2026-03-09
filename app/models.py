from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Chapter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    url: str
    index: int
    content_html: str | None = None

    @field_validator("url")
    @classmethod
    def url_must_be_http(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"url must start with http:// or https://, got: {value!r}")
        return value


class Book(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    url: str
    author: str | None = None
    cover_url: str | None = None
    source: str
    chapters: list[Chapter] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def url_must_be_http(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"url must start with http:// or https://, got: {value!r}")
        return value

    @field_validator("cover_url")
    @classmethod
    def cover_url_must_be_http(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError(f"cover_url must start with http:// or https://, got: {value!r}")
        return value
