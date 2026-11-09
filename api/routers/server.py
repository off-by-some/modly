"""Server-owned state exposed to desktop clients."""

import json
import os
import re
import tempfile
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

try:
    import psutil
except ModuleNotFoundError:  # A just-upgraded server can still report basic Unix memory before deps are refreshed.
    psutil = None
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel


router = APIRouter(tags=["server"])

_workspace_env = os.environ.get("WORKSPACE_DIR")
WORKFLOWS_DIR = Path(
    os.environ.get("WORKFLOWS_DIR")
    or (Path(_workspace_env).parent / "workflows" if _workspace_env else Path.home() / ".modly" / "workflows")
)
WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _workflow_path(workflow_id: str) -> Path:
    if not _SAFE_ID.fullmatch(workflow_id):
        raise HTTPException(400, "Invalid workflow id")
    return WORKFLOWS_DIR / f"{workflow_id}.json"


class WorkflowDocument(BaseModel):
    id: str
    name: str = "Untitled Workflow"
    description: str = ""
    folder: str | None = None
    bookmarked: bool | None = None
    nodes: list[dict]
    edges: list[dict]
    createdAt: str
    updatedAt: str


class CollectionRename(BaseModel):
    old_name: str
    new_name: str


class JobMetadata(BaseModel):
    filename: str
    meta: dict


class DirectoryListing(BaseModel):
    path: str
    extensions: list[str] | None = None


def _workspace_root() -> Path:
    import services.generator_registry as registry
    registry.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    return registry.WORKSPACE_DIR.resolve()


def _safe_name(value: str, label: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise HTTPException(400, f"Invalid {label}")
    return value


def _workspace_asset_path(value: str) -> tuple[str, Path]:
    normalized = value.replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part]
    if not parts or parts[0] not in {"Workflows", "Exports"} or any(part in {".", ".."} for part in parts):
        raise HTTPException(400, "Workspace path must stay under Workflows or Exports")
    root = _workspace_root()
    path = (root / normalized).resolve()
    if root not in path.parents:
        raise HTTPException(400, "Workspace path escapes the server workspace")
    return normalized, path


def _workspace_directory(value: str) -> Path:
    root = _workspace_root()
    candidate = Path(value)
    path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if path != root and root not in path.parents:
        raise HTTPException(400, "Directory escapes the server workspace")
    return path


def _asset_entry(workspace_path: str, path: Path) -> dict:
    suffix = path.suffix.lower()
    is_model = suffix in {".glb", ".gltf"}
    is_mesh = suffix in {".glb", ".gltf", ".obj", ".stl", ".ply", ".splat"}
    is_text = suffix in {".json", ".txt", ".md"}
    capability = "mesh" if is_mesh else None
    if workspace_path.endswith(".world.json"):
        capability = "generated-world"
    elif workspace_path.endswith(".scene.json") or workspace_path.endswith("/scene-manifest.json"):
        capability = "scene-manifest"
    stats = path.stat()
    return {
        "id": f"library:{workspace_path}",
        "workspacePath": workspace_path,
        "displayName": path.name,
        "sourceScope": "exports" if workspace_path.startswith("Exports/") else "workflows",
        "capability": capability,
        "state": "ready" if capability else "unsupported",
        "previewKind": "3d-model" if is_model else "text" if is_text else "binary",
        "warnings": [],
        "openable": is_model,
        "nonOpenableReason": None if is_model else "This server workspace asset is list-only.",
        "createdAt": datetime.fromtimestamp(stats.st_ctime, timezone.utc).isoformat(),
        "updatedAt": datetime.fromtimestamp(stats.st_mtime, timezone.utc).isoformat(),
    }


@router.get("/system/memory")
async def system_memory():
    if psutil is not None:
        memory = psutil.virtual_memory()
        return {
            "total": memory.total,
            "used": memory.used,
            "available": memory.available,
        }
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        available = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES")
        return {"total": total, "used": total - available, "available": available}
    except (AttributeError, OSError, ValueError):
        raise HTTPException(503, "Server memory information is unavailable")


@router.get("/workflows")
async def list_workflows():
    workflows = []
    for path in WORKFLOWS_DIR.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and value.get("id"):
                workflows.append(value)
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(workflows, key=lambda item: item.get("updatedAt", ""), reverse=True)


@router.put("/workflows/{workflow_id}")
async def save_workflow(workflow_id: str, workflow: WorkflowDocument):
    if workflow.id != workflow_id:
        raise HTTPException(400, "Workflow id does not match the request path")
    destination = _workflow_path(workflow_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{workflow_id}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(workflow.model_dump(exclude_none=True), handle, indent=2)
        os.replace(temporary_name, destination)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    return {"success": True}


@router.delete("/workflows/{workflow_id}")
async def delete_workflow(workflow_id: str):
    _workflow_path(workflow_id).unlink(missing_ok=True)
    return {"success": True}


@router.get("/workspace-admin/collections")
async def list_collections():
    return [path.name for path in _workspace_root().iterdir() if path.is_dir()]


@router.post("/workspace-admin/upload")
async def upload_workspace_file(request: Request, x_modly_filename: str = Header(...)):
    """Stage a desktop-selected workflow input in this server's workspace."""
    file_name = Path(unquote(x_modly_filename)).name
    if not file_name or file_name in {".", ".."}:
        raise HTTPException(422, "Invalid upload filename")
    upload_dir = _workspace_root() / "tmp" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    destination = upload_dir / f"{uuid.uuid4().hex}-{file_name}"
    partial = destination.with_suffix(destination.suffix + ".uploading")
    try:
        with partial.open("xb") as output:
            async for chunk in request.stream():
                if chunk:
                    output.write(chunk)
        partial.replace(destination)
    except Exception as exc:
        partial.unlink(missing_ok=True)
        raise HTTPException(500, f"Could not stage workflow input: {exc}")
    return {
        "file_path": str(destination),
        "workspace_path": destination.relative_to(_workspace_root()).as_posix(),
    }


@router.post("/workspace-admin/list-files")
async def list_workspace_files(body: DirectoryListing):
    directory = _workspace_directory(body.path)
    if not directory.is_dir():
        return []
    wanted = {value.lower().lstrip(".") for value in (body.extensions or [])}
    return sorted(
        path.name for path in directory.iterdir()
        if path.is_file() and (not wanted or path.suffix.lower().lstrip(".") in wanted)
    )


@router.delete("/workspace-admin/tmp")
async def clear_workspace_tmp():
    tmp_dir = _workspace_root() / "tmp"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return {"success": True}


@router.post("/workspace-admin/collections/{name}")
async def create_collection(name: str):
    (_workspace_root() / _safe_name(name, "collection name")).mkdir(parents=True, exist_ok=True)
    return {"success": True}


@router.post("/workspace-admin/rename-collection")
async def rename_collection(body: CollectionRename):
    source = _workspace_root() / _safe_name(body.old_name, "collection name")
    destination = _workspace_root() / _safe_name(body.new_name, "collection name")
    source.rename(destination)
    return {"success": True}


@router.delete("/workspace-admin/collections/{name}")
async def delete_collection(name: str):
    shutil.rmtree(_workspace_root() / _safe_name(name, "collection name"), ignore_errors=True)
    return {"success": True}


@router.get("/workspace-admin/collections/{name}/jobs")
async def list_jobs(name: str):
    collection = _workspace_root() / _safe_name(name, "collection name")
    result = []
    for path in collection.glob("*.meta.json") if collection.exists() else []:
        try:
            result.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return result


@router.put("/workspace-admin/collections/{name}/jobs")
async def save_job_metadata(name: str, body: JobMetadata):
    collection = _workspace_root() / _safe_name(name, "collection name")
    collection.mkdir(parents=True, exist_ok=True)
    filename = Path(body.filename).name
    meta_name = re.sub(r"\.glb$", ".meta.json", filename, flags=re.IGNORECASE)
    (collection / meta_name).write_text(json.dumps(body.meta, indent=2), encoding="utf-8")
    return {"success": True}


@router.delete("/workspace-admin/collections/{name}/jobs/{filename}")
async def delete_job(name: str, filename: str):
    collection = _workspace_root() / _safe_name(name, "collection name")
    safe_filename = Path(filename).name
    (collection / safe_filename).unlink(missing_ok=True)
    (collection / re.sub(r"\.glb$", ".meta.json", safe_filename, flags=re.IGNORECASE)).unlink(missing_ok=True)
    return {"success": True}


@router.get("/workspace-library")
async def list_workspace_library():
    entries = []
    for scope in ("Workflows", "Exports"):
        root = _workspace_root() / scope
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and not path.name.startswith(".") and not path.name.endswith((".artifact.json", ".rigmeta.json")):
                workspace_path = path.relative_to(_workspace_root()).as_posix()
                entries.append(_asset_entry(workspace_path, path))
    return {"success": True, "entries": entries}


@router.post("/workspace-library/read")
async def read_workspace_library(payload: dict):
    workspace_path, path = _workspace_asset_path(str(payload.get("workspacePath", "")))
    if not path.is_file():
        return {"success": False, "error": {"code": "not-found", "message": "Workspace asset was not found."}}
    entry = _asset_entry(workspace_path, path)
    if entry["previewKind"] == "3d-model":
        preview = {"kind": "3d-model", "viewerKind": path.suffix.lower().lstrip(".")}
    elif entry["previewKind"] == "text":
        raw = path.read_bytes()
        preview = {"kind": "text", "content": raw[:1_000_000].decode("utf-8", errors="replace"), "byteLength": len(raw), "truncated": len(raw) > 1_000_000}
    else:
        preview = {"kind": "binary", "binaryKind": path.suffix.lower().lstrip("."), "byteLength": path.stat().st_size, "message": "Binary server workspace asset."}
    return {"success": True, "entry": entry, "preview": preview}


@router.post("/workspace-library/open")
async def open_workspace_library(payload: dict):
    workspace_path, path = _workspace_asset_path(str(payload.get("workspacePath", "")))
    if not path.is_file():
        return {"success": False, "error": {"code": "not-found", "message": "Workspace asset was not found."}}
    entry = _asset_entry(workspace_path, path)
    if not entry["openable"]:
        return {"success": False, "error": {"code": "not-openable", "message": entry["nonOpenableReason"]}}
    return {"success": True, "entry": entry}


@router.get("/logs/sessions")
async def list_log_sessions():
    return []


@router.get("/logs")
async def read_logs():
    log_dir = Path(os.environ.get("MODLY_LOG_DIR") or Path.home() / ".modly" / "logs")
    server_log = log_dir / "server.log"
    try:
        content = server_log.read_text(encoding="utf-8")[-2_000_000:]
    except OSError:
        content = ""
    return {"runtime.log": content, "errors.log": content, "modly.log": ""}
