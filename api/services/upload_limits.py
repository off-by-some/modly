"""Bounded readers for the few assets that must originate on a client."""

from fastapi import HTTPException, UploadFile


async def read_upload_limited(upload: UploadFile, limit: int, label: str) -> bytes:
    data = bytearray()
    while chunk := await upload.read(min(1024 * 1024, limit + 1 - len(data))):
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(413, f"{label} exceeds the server upload limit")
    return bytes(data)

