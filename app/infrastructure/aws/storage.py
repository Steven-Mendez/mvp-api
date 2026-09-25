"""The media bucket as the `MediaStorage` port.

Clients upload straight to S3 with a presigned POST under `uploads/` (a lifecycle rule
drops what is never confirmed after a day). Confirming validates and re-encodes the upload
with Pillow into `media/`, which CloudFront serves at `MEDIA_BASE_URL`.
"""

import asyncio
import io
import uuid

from botocore.exceptions import ClientError
from loguru import logger
from PIL import Image, ImageOps, UnidentifiedImageError

from app.application.ports import StoredImage, UploadTicket
from app.domain.errors import (
    ContentTooLargeError,
    InvalidError,
    NotFoundError,
    UnsupportedMediaTypeError,
)
from app.infrastructure.aws.clients import s3

UPLOAD_PREFIX = "uploads/"
MEDIA_PREFIX = "media/"
PRESIGN_EXPIRES_SECONDS = 600
SQUARE_SIZE = 256
MAX_IMAGE_SIDE = 2048
UPLOAD_NOT_FOUND = "Upload not found: it was never uploaded, expired or was already used"

# Pillow refuses images above this many pixels (decompression bombs).
Image.MAX_IMAGE_PIXELS = 40_000_000


class S3MediaStorage:
    def __init__(
        self, bucket: str, base_url: str, max_upload_bytes: int, thumbnail_size: int
    ) -> None:
        self._bucket = bucket
        self._base_url = base_url.rstrip("/")
        self._max_bytes = max_upload_bytes
        self._thumbnail_size = thumbnail_size

    def presign(self, content_type: str, allowed: frozenset[str]) -> UploadTicket:
        if content_type not in allowed:
            raise UnsupportedMediaTypeError(
                f"Unsupported image type; use one of {', '.join(sorted(allowed))}"
            )
        key = f"{UPLOAD_PREFIX}{uuid.uuid4().hex}"
        post = s3().generate_presigned_post(
            Bucket=self._bucket,
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 1, self._max_bytes],
            ],
            ExpiresIn=PRESIGN_EXPIRES_SECONDS,
        )
        return UploadTicket(
            url=post["url"],
            fields=post["fields"],
            key=key.removeprefix(UPLOAD_PREFIX),
            max_bytes=self._max_bytes,
            expires_in=PRESIGN_EXPIRES_SECONDS,
        )

    async def pending_size(self, upload_key: str) -> int:
        try:
            head = await asyncio.to_thread(
                s3().head_object, Bucket=self._bucket, Key=self._pending(upload_key)
            )
        except ClientError as exc:
            raise NotFoundError(UPLOAD_NOT_FOUND) from exc
        size = head["ContentLength"]
        if size > self._max_bytes:
            await self.delete(self._pending(upload_key))
            raise ContentTooLargeError("The upload is larger than allowed")
        return size

    async def store_image(self, upload_key: str) -> StoredImage:
        original = await self._read_pending(upload_key)
        full, thumb = await asyncio.to_thread(self._render_image, original)
        stem = f"{MEDIA_PREFIX}{uuid.uuid4().hex}"
        key, thumb_key = f"{stem}.webp", f"{stem}_thumb.webp"
        await self._put(key, full)
        await self._put(thumb_key, thumb)
        await self.delete(self._pending(upload_key))
        return StoredImage(
            key=key, thumb_key=thumb_key, content_type="image/webp", size_bytes=len(full)
        )

    async def store_square(self, upload_key: str) -> str:
        original = await self._read_pending(upload_key)
        square = await asyncio.to_thread(self._render_square, original)
        key = f"{MEDIA_PREFIX}{uuid.uuid4().hex}.webp"
        await self._put(key, square)
        await self.delete(self._pending(upload_key))
        return key

    def url_for(self, key: str) -> str:
        return f"{self._base_url}/{key}"

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(s3().delete_object, Bucket=self._bucket, Key=key)
        except ClientError:
            logger.opt(exception=True).warning("Could not delete media object {}", key)

    # --- helpers ------------------------------------------------------------------------

    @staticmethod
    def _pending(upload_key: str) -> str:
        if not upload_key.isalnum():
            raise NotFoundError(UPLOAD_NOT_FOUND)
        return f"{UPLOAD_PREFIX}{upload_key}"

    async def _read_pending(self, upload_key: str) -> bytes:
        await self.pending_size(upload_key)
        response = await asyncio.to_thread(
            s3().get_object, Bucket=self._bucket, Key=self._pending(upload_key)
        )
        return await asyncio.to_thread(response["Body"].read)

    async def _put(self, key: str, body: bytes) -> None:
        await asyncio.to_thread(
            s3().put_object,
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType="image/webp",
            CacheControl="public, max-age=31536000, immutable",
        )

    def _render_image(self, data: bytes) -> tuple[bytes, bytes]:
        image = _open(data)
        image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        full = _webp(image)
        image.thumbnail((self._thumbnail_size, self._thumbnail_size))
        return full, _webp(image)

    @staticmethod
    def _render_square(data: bytes) -> bytes:
        image = ImageOps.fit(_open(data), (SQUARE_SIZE, SQUARE_SIZE))
        return _webp(image)


def _open(data: bytes) -> Image.Image:
    """Decode an upload whatever it claimed to be; only real images get through."""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise InvalidError("The upload is not a valid image") from exc
    if image.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
        raise UnsupportedMediaTypeError("Unsupported image type")
    image = ImageOps.exif_transpose(image)
    return image.convert("RGBA" if image.has_transparency_data else "RGB")


def _webp(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", quality=82, method=4)
    return buffer.getvalue()
