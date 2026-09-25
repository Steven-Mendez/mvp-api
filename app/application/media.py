"""Which images each upload flow accepts."""

PRODUCT_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
# Avatars and logos.
SQUARE_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
