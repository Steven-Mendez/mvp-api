"""The request and response bodies of the presign-then-confirm upload flow."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.application.ports import UploadTicket


class UploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_type: str = Field(max_length=64, description="The file's MIME type.")


class UploadConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(max_length=64, description="The `key` the upload request answered with.")


class PresignedUploadRead(BaseModel):
    """What the client needs for one multipart POST straight to the bucket."""

    url: str
    fields: dict[str, str]
    key: str
    max_bytes: int
    expires_in: int

    @classmethod
    def of(cls, ticket: UploadTicket) -> PresignedUploadRead:
        return cls(
            url=ticket.url,
            fields=ticket.fields,
            key=ticket.key,
            max_bytes=ticket.max_bytes,
            expires_in=ticket.expires_in,
        )
