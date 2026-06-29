"""Small shared helpers for the local solver tools."""

MAX_OUTPUT = 24_000


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    head = "\n".join(lines[:200])
    return head[:limit] + f"\n... [truncated - {len(text)} total chars, {len(lines)} lines]"


IMAGE_EXTS_FOR_VISION: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".webp": "image/webp",
}

IMAGE_MAGIC: dict[str, list[int]] = {
    "image/png": [0x89, 0x50, 0x4E, 0x47],
    "image/jpeg": [0xFF, 0xD8, 0xFF],
    "image/gif": [0x47, 0x49, 0x46],
    "image/bmp": [0x42, 0x4D],
    "image/webp": [0x52, 0x49, 0x46, 0x46],
}


def _has_valid_magic(data: bytes, mime_type: str) -> bool:
    magic = IMAGE_MAGIC.get(mime_type)
    if not magic:
        return True
    return all(i < len(data) and data[i] == b for i, b in enumerate(magic))
