import os

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    """Health check — used by Electron to know the API is ready."""
    authenticated = bool(os.environ.get("MODLY_SERVER_TOKEN", "").strip())
    response = {"status": "ok", "authentication": "required" if authenticated else "disabled"}
    if not authenticated:
        response["warning"] = "MODLY_SERVER_TOKEN is not set; this API accepts unauthenticated requests."
    return response
