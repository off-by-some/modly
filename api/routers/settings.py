import asyncio
import os
from fastapi import APIRouter
from pydantic import BaseModel
from pathlib import Path
from typing import Optional

import services.generator_registry as reg_module
from routers import server as server_router

router = APIRouter(prefix="/settings", tags=["settings"])


class PathsUpdate(BaseModel):
    models_dir:    Optional[str] = None
    workspace_dir: Optional[str] = None
    workflows_dir: Optional[str] = None
    extensions_dir: Optional[str] = None


class TokenUpdate(BaseModel):
    token: str


@router.get("/paths")
async def get_paths():
    return {
        "models_dir":    str(reg_module.MODELS_DIR),
        "workspace_dir": str(reg_module.WORKSPACE_DIR),
        "workflows_dir": str(server_router.WORKFLOWS_DIR),
        "extensions_dir": str(reg_module.EXTENSIONS_DIR or ""),
    }


@router.post("/paths")
async def update_paths(body: PathsUpdate):
    # Off the event loop: changing paths waits for any in-progress model load.
    await asyncio.to_thread(
        reg_module.generator_registry.update_paths,
        Path(body.models_dir)    if body.models_dir    else None,
        Path(body.workspace_dir) if body.workspace_dir else None,
    )
    if body.workflows_dir:
        server_router.WORKFLOWS_DIR = Path(body.workflows_dir)
        server_router.WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    if body.extensions_dir:
        reg_module.EXTENSIONS_DIR = Path(body.extensions_dir)
        reg_module.EXTENSIONS_DIR.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(reg_module.generator_registry.reload)
    return {
        "models_dir":    str(reg_module.MODELS_DIR),
        "workspace_dir": str(reg_module.WORKSPACE_DIR),
        "workflows_dir": str(server_router.WORKFLOWS_DIR),
        "extensions_dir": str(reg_module.EXTENSIONS_DIR or ""),
    }


@router.post("/hf-token")
async def update_hf_token(body: TokenUpdate):
    """
    Update the HuggingFace token in this process's environment so that
    extension subprocesses spawned after this call inherit the new token.
    """
    if body.token:
        os.environ["HUGGING_FACE_HUB_TOKEN"] = body.token
        os.environ["HF_TOKEN"]               = body.token
    else:
        os.environ.pop("HUGGING_FACE_HUB_TOKEN", None)
        os.environ.pop("HF_TOKEN", None)
    return {"ok": True}
