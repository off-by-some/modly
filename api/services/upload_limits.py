"""Optional, environment-configured limits for client-originated assets."""

import os

from fastapi import HTTPException, UploadFile


def optional_env_limit(name: str, fallback_name: str | None = None) -> int | None:
    """Unset, zero, `none`, or `unlimited` means no limit."""
    raw_value = os.environ.get(name)
    if raw_value is None and fallback_name:
        raw_value = os.environ.get(fallback_name)
    raw = (raw_value or "").strip().lower()
    if raw in {"", "0", "none", "unlimited"}:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer byte count, 0, or unlimited") from exc
    if value < 0:
        raise ValueError(f"{name} must be a positive integer byte count, 0, or unlimited")
    return value or None


async def read_upload_limited(upload: UploadFile, limit: int | None, label: str) -> bytes:
    data = bytearray()
    while chunk := await upload.read(
        1024 * 1024 if limit is None else min(1024 * 1024, limit + 1 - len(data))
    ):
        data.extend(chunk)
        if limit is not None and len(data) > limit:
            raise HTTPException(413, f"{label} exceeds the server upload limit")
    return bytes(data)

