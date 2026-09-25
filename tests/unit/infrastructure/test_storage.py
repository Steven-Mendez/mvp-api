"""The media bucket adapter, without S3.

- an image over the pixel limit is refused from its header, before it is decoded
- a small real image still decodes
- only a missing upload is a 404; throttling or a 5xx stays an error the client can retry
"""

import io

import pytest
from botocore.exceptions import ClientError
from PIL import Image

from app.domain.errors import InvalidError, NotFoundError
from app.infrastructure.aws import storage
from app.infrastructure.aws.storage import S3MediaStorage

_open = storage._open  # pyright: ignore[reportPrivateUsage]


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# --- decoding ---------------------------------------------------------------------------


# In production Pillow only warns between the limit and twice it; ignoring the warning here
# reproduces that, so the rejection must come from `_open`'s own check.
@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_an_image_over_the_pixel_limit_is_refused_before_decoding() -> None:
    bomb = _png(Image.new("1", (7000, 7000)))  # 49M pixels: under Pillow's own error

    with pytest.raises(InvalidError, match="not a valid image"):
        _open(bomb)


def test_the_pixel_limit_holds_when_warnings_are_errors() -> None:
    bomb = _png(Image.new("1", (7000, 7000)))

    with pytest.raises(InvalidError, match="not a valid image"):
        _open(bomb)


def test_a_small_png_is_decoded() -> None:
    image = _open(_png(Image.new("RGB", (8, 6), "red")))

    assert image.size == (8, 6)
    assert image.mode == "RGB"


# --- a missing upload -------------------------------------------------------------------


class _S3:
    def __init__(self, code: str) -> None:
        self._code = code

    def head_object(self, **_: object) -> dict[str, object]:
        raise ClientError({"Error": {"Code": self._code, "Message": ""}}, "HeadObject")


def _storage(monkeypatch: pytest.MonkeyPatch, code: str) -> S3MediaStorage:
    monkeypatch.setattr(storage, "s3", lambda: _S3(code))
    return S3MediaStorage("media", "https://cdn.example.com", 5_000_000, 256)


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
async def test_a_missing_upload_is_not_found(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    with pytest.raises(NotFoundError):
        await _storage(monkeypatch, code).pending_size("abc123")


async def test_throttling_is_not_presented_as_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ClientError) as caught:
        await _storage(monkeypatch, "SlowDown").pending_size("abc123")

    assert caught.value.response.get("Error", {}).get("Code") == "SlowDown"


class _ConsumedBetweenHeadAndGet:
    def head_object(self, **_: object) -> dict[str, object]:
        return {"ContentLength": 10}

    def get_object(self, **_: object) -> dict[str, object]:
        raise ClientError({"Error": {"Code": "NoSuchKey", "Message": ""}}, "GetObject")


async def test_an_upload_consumed_before_it_is_read_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "s3", _ConsumedBetweenHeadAndGet)
    media = S3MediaStorage("media", "https://cdn.example.com", 5_000_000, 256)

    with pytest.raises(NotFoundError):
        await media.store_square("abc123")
