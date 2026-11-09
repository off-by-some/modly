import asyncio
import json
import time
import os
import socket
import threading
import shutil
import re
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from fastapi import APIRouter, Header, HTTPException, Request as FastAPIRequest
from fastapi.responses import StreamingResponse
from services.generator_registry import generator_registry
import services.generator_registry as registry_module
from services.extension_process import ExtensionProcess
from services.model_sources import (
    normalize_model_sources,
    resolve_download_path,
    resolve_source_destination_at_root,
    resolve_weight_storage_root,
    validate_source_file_plan,
)

router = APIRouter(tags=["model"])

_SAFE_MODEL_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _safe_model_parts(model_id: str) -> tuple[str, str]:
    parts = model_id.split("/")
    if len(parts) != 2 or not all(_SAFE_MODEL_PART.fullmatch(part) for part in parts):
        raise HTTPException(400, "Model id must identify one safe extension node")
    return parts[0], parts[1]


def _model_root(model_id: str) -> Path:
    extension_id, node_id = _safe_model_parts(model_id)
    return registry_module.MODELS_DIR / extension_id / node_id


def _storage_has_data(root: Path) -> bool:
    return root.exists() and any(
        path.is_file() and path.stat().st_size > 0 and ".cache" not in path.parts
        for path in root.rglob("*")
    )


def _installed_variants(model_id: str, manifest: dict) -> list[str] | None:
    variants = manifest.get("weight_variants")
    if not isinstance(variants, dict):
        return None
    root = _model_root(model_id)
    installed = []
    for option in variants.get("options", []):
        if not isinstance(option, dict) or not isinstance(option.get("id"), str):
            continue
        prefixes = option.get("include_prefixes") or []
        files = [path for path in root.rglob("*") if path.is_file() and path.stat().st_size > 0]
        if prefixes and all(any(
            path.relative_to(root).as_posix().startswith(prefix)
            for path in files
        ) for prefix in prefixes):
            installed.append(option["id"])
    return installed


@router.get("/storage/{model_id:path}/state")
async def model_storage_state(model_id: str):
    try:
        manifest = generator_registry.get_manifest(model_id)
    except KeyError as exc:
        raise HTTPException(404, f"Unknown model ID: {model_id}") from exc
    statuses = {item["id"]: item for item in generator_registry.all_status()}
    return {
        "downloaded": bool(statuses.get(model_id, {}).get("downloaded", False)),
        "hasLocalData": _storage_has_data(_model_root(model_id)),
        "installedWeightVariants": _installed_variants(model_id, manifest),
    }


@router.get("/storage/extensions/{extension_id}/shared-groups")
async def shared_group_states(extension_id: str):
    if not _SAFE_MODEL_PART.fullmatch(extension_id):
        raise HTTPException(400, "Invalid extension id")
    groups: dict[str, dict] = {}
    for model_id, manifest in generator_registry._manifests.items():
        if not model_id.startswith(f"{extension_id}/"):
            continue
        for group in manifest.get("weight_groups", []) or []:
            if not isinstance(group, dict) or not isinstance(group.get("id"), str):
                continue
            entry = groups.setdefault(group["id"], {"group": group, "dependents": []})
            entry["dependents"].append(model_id)
    result = []
    for group_id, entry in groups.items():
        target_id = f"{extension_id}/_shared/{group_id}"
        root = registry_module.MODELS_DIR / extension_id / "_shared" / group_id
        checks = [
            check
            for source in entry["group"].get("model_sources", [])
            for check in source.get("checks", [])
        ]
        result.append({
            "id": group_id,
            "targetId": target_id,
            "dependentModelIds": entry["dependents"],
            "downloaded": bool(checks) and all((root / check).is_file() for check in checks),
            "hasLocalData": _storage_has_data(root),
        })
    return result


def _download_source(
    source: dict,
    destination: Path,
    token: str | None,
    control: dict[str, threading.Event],
    on_progress,
) -> None:
    from huggingface_hub import hf_hub_url, list_repo_files
    destination.mkdir(parents=True, exist_ok=True)
    include = source.get("include_prefixes") or []
    skip = source.get("skip_prefixes") or []
    files = [
        name for name in list_repo_files(
            source["repo_id"], revision=source.get("revision"), token=token
        )
        if not include or any(name.startswith(prefix) for prefix in include)
        if not any(name.startswith(prefix) for prefix in skip)
    ]
    if not files:
        raise RuntimeError(f'No files matched the configured prefixes in {source["repo_id"]}')
    for index, filename in enumerate(files, 1):
        _check_managed_download(control)
        try:
            _download_file_streamed(
                url=hf_hub_url(
                    repo_id=source["repo_id"],
                    filename=filename,
                    revision=source.get("revision"),
                ),
                filename=filename,
                dest_dir=str(destination),
                file_index=index,
                total_files=len(files),
                base_percent=0,
                progress_cb=lambda message, i=index: on_progress(
                    i, len(files), filename, message.get("percent", 0)
                ),
                control=control,
                token=token,
            )
        except DownloadPaused as exc:
            raise ManagedDownloadPaused() from exc
        except DownloadCancelled as exc:
            raise ManagedDownloadCancelled() from exc
        on_progress(index, len(files), filename, 100)
        _check_managed_download(control)


class ManagedDownloadPaused(Exception):
    pass


class ManagedDownloadCancelled(Exception):
    pass


_managed_download_controls: dict[str, dict] = {}


def _check_managed_download(control: dict[str, threading.Event]) -> None:
    if control["cancel"].is_set():
        raise ManagedDownloadCancelled()
    if control["pause"].is_set():
        raise ManagedDownloadPaused()


@router.post("/managed-download")
async def managed_download(model_id: str, variant_id: str | None = None):
    try:
        manifest = generator_registry.get_manifest(model_id)
    except KeyError as exc:
        raise HTTPException(404, f"Unknown model ID: {model_id}") from exc
    extension_id, _ = _safe_model_parts(model_id)
    token = os.environ.get("HUGGING_FACE_HUB_TOKEN") or os.environ.get("HF_TOKEN")
    root = _model_root(model_id)
    if model_id in _managed_download_controls:
        raise HTTPException(409, "Download already in progress")
    control = {
        "pause": threading.Event(),
        "cancel": threading.Event(),
        "percent": 0,
        "status": "Listing server download…",
    }
    _managed_download_controls[model_id] = control

    def download() -> None:
        completed_sources = 0
        source_count = sum(
            len(group.get("model_sources", []) or [])
            for group in manifest.get("weight_groups", []) or []
        ) + len(manifest.get("model_sources", []) or [])
        if not isinstance(manifest.get("model_sources"), list) and manifest.get("hf_repo"):
            source_count += 2 if isinstance(manifest.get("weight_variants"), dict) else 1

        def download_source(source: dict, destination: Path) -> None:
            nonlocal completed_sources
            start = completed_sources / max(1, source_count)

            def report(index: int, total: int, filename: str, file_percent: int) -> None:
                completed_files = (index - 1 + max(0, min(100, file_percent)) / 100) / total
                control["percent"] = round((start + completed_files / max(1, source_count)) * 100)
                control["status"] = f"Downloading {filename}"

            _download_source(source, destination, token, control, report)
            completed_sources += 1

        for group in manifest.get("weight_groups", []) or []:
            group_root = registry_module.MODELS_DIR / extension_id / "_shared" / group["id"]
            for source in group.get("model_sources", []) or []:
                _check_managed_download(control)
                destination = group_root / source.get("destination", ".")
                download_source(source, destination)
                _check_managed_download(control)

        sources = manifest.get("model_sources")
        if isinstance(sources, list):
            for source in sources:
                _check_managed_download(control)
                destination = root / source.get("destination", ".")
                download_source(source, destination)
                _check_managed_download(control)
            return

        repo_id = manifest.get("hf_repo")
        if not repo_id:
            raise RuntimeError(f"Model {model_id} has no download source")
        variants = manifest.get("weight_variants")
        include = list(manifest.get("hf_include_prefixes") or [])
        skip = list(manifest.get("hf_skip_prefixes") or [])
        if isinstance(variants, dict):
            selected = variant_id or variants.get("default")
            options = [option for option in variants.get("options", []) if option.get("id") == selected]
            if len(options) != 1:
                raise RuntimeError(f"Unknown weight variant: {selected}")
            variant_prefixes = [
                prefix
                for option in variants.get("options", [])
                for prefix in option.get("include_prefixes", [])
            ]
            common_source = {
                "repo_id": repo_id,
                "include_prefixes": include,
                "skip_prefixes": [*skip, *variant_prefixes],
            }
            _check_managed_download(control)
            download_source(common_source, root)
            _check_managed_download(control)
            include = options[0].get("include_prefixes", [])
        _check_managed_download(control)
        download_source({
            "repo_id": repo_id,
            "include_prefixes": include,
            "skip_prefixes": skip,
        }, root)
        _check_managed_download(control)

    try:
        await asyncio.to_thread(download)
    except ManagedDownloadPaused:
        return {"success": False, "paused": True}
    except ManagedDownloadCancelled:
        extension_root = registry_module.MODELS_DIR / extension_id
        for partial in extension_root.rglob("*.part") if extension_root.exists() else []:
            partial.unlink(missing_ok=True)
        return {"success": False, "cancelled": True}
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    finally:
        if _managed_download_controls.get(model_id) is control:
            _managed_download_controls.pop(model_id, None)
    return {"success": True}


@router.get("/managed-downloads")
async def managed_downloads():
    return [
        {
            "modelId": model_id,
            "percent": control.get("percent", 0),
            "status": control.get("status", "Downloading on server…"),
        }
        for model_id, control in _managed_download_controls.items()
    ]


@router.post("/managed-download/pause")
async def pause_managed_download(model_id: str):
    control = _managed_download_controls.get(model_id)
    if control is None:
        raise HTTPException(404, "No active download")
    control["pause"].set()
    return {"success": True}


@router.post("/managed-download/cancel")
async def cancel_managed_download(model_id: str):
    control = _managed_download_controls.get(model_id)
    if control is None:
        raise HTTPException(404, "No active download")
    control["cancel"].set()
    return {"success": True}


@router.delete("/storage/model/{model_id:path}")
async def delete_model_storage(model_id: str):
    await unload_model(model_id)
    shutil.rmtree(_model_root(model_id), ignore_errors=True)
    return {"success": True}


@router.delete("/storage/{model_id:path}/variants/{variant_id}")
async def delete_model_variant(model_id: str, variant_id: str):
    manifest = generator_registry.get_manifest(model_id)
    variants = manifest.get("weight_variants") or {}
    option = next((item for item in variants.get("options", []) if item.get("id") == variant_id), None)
    if not option:
        raise HTTPException(404, "Unknown weight variant")
    await unload_model(model_id)
    root = _model_root(model_id)
    for prefix in option.get("include_prefixes", []):
        candidate = (root / prefix).resolve()
        if root.resolve() not in candidate.parents and candidate != root.resolve():
            raise HTTPException(400, "Unsafe variant path")
        if candidate.is_dir():
            shutil.rmtree(candidate)
        else:
            candidate.unlink(missing_ok=True)
    return {"success": True}


@router.delete("/storage/extensions/{extension_id}")
async def delete_extension_storage(extension_id: str):
    if not _SAFE_MODEL_PART.fullmatch(extension_id):
        raise HTTPException(400, "Invalid extension id")
    model_ids = [model_id for model_id in generator_registry._generators if model_id.startswith(f"{extension_id}/")]
    for model_id in model_ids:
        await unload_model(model_id)
    shutil.rmtree(registry_module.MODELS_DIR / extension_id, ignore_errors=True)
    return {"success": True}


@router.delete("/storage/extensions/{extension_id}/shared-groups/{group_id}")
async def delete_shared_group_storage(extension_id: str, group_id: str):
    if not _SAFE_MODEL_PART.fullmatch(extension_id) or not _SAFE_MODEL_PART.fullmatch(group_id):
        raise HTTPException(400, "Invalid shared group id")
    states = await shared_group_states(extension_id)
    state = next((item for item in states if item["id"] == group_id), None)
    if state is None:
        raise HTTPException(404, "Unknown shared weight group")
    for model_id in state["dependentModelIds"]:
        await unload_model(model_id)
    shutil.rmtree(registry_module.MODELS_DIR / extension_id / "_shared" / group_id, ignore_errors=True)
    return {"success": True}


class DownloadPaused(Exception):
    pass


class DownloadCancelled(Exception):
    pass


_download_controls: dict[str, dict[str, threading.Event]] = {}


def _download_control(model_id: str) -> dict[str, threading.Event]:
    """Return the current control for pause/cancel endpoints."""
    control = _download_controls.get(model_id)
    if control is None:
        control = {"pause": threading.Event(), "cancel": threading.Event()}
        _download_controls[model_id] = control
    return control


def _new_download_control(model_id: str) -> dict[str, threading.Event]:
    """Create a fresh control for a new download session."""
    control: dict[str, threading.Event] = {"pause": threading.Event(), "cancel": threading.Event()}
    _download_controls[model_id] = control
    return control


def _check_download_control(control: dict[str, threading.Event]) -> None:
    if control["cancel"].is_set():
        raise DownloadCancelled()
    if control["pause"].is_set():
        raise DownloadPaused()


@router.get("/status")
async def model_status():
    """Status of the active model."""
    return await asyncio.to_thread(generator_registry.active_status)


@router.get("/all")
async def all_models_status():
    """Status of all known models (downloaded, loaded, required VRAM)."""
    return await asyncio.to_thread(generator_registry.all_status)


@router.get("/params")
async def model_params(model_id: Optional[str] = None):
    """Parameter schema of the active model (or a specified model)."""
    try:
        return await asyncio.to_thread(generator_registry.params_schema, model_id)
    except KeyError:
        raise HTTPException(404, f"Unknown model ID: {model_id}")


@router.post("/switch")
async def switch_model(model_id: str):
    """Switch the active model."""
    try:
        await asyncio.to_thread(generator_registry.switch_model, model_id)
        return {"active": model_id}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/unload-all")
async def unload_all_models():
    """Unloads all models from memory to free VRAM/RAM."""
    # Off the event loop: unloading waits for any in-progress model load.
    await asyncio.to_thread(generator_registry.unload_all)
    # The local LLMs hold VRAM too; "Free memory" left them resident.
    from services.llm_server import llama_pool
    await asyncio.to_thread(llama_pool.unload_all)
    # Force Python to release memory back to the OS
    import gc
    gc.collect()
    try:
        import ctypes, sys
        if sys.platform == "win32":
            k32 = ctypes.windll.kernel32
            k32.SetProcessWorkingSetSizeEx(k32.GetCurrentProcess(), -1, -1, 0)
    except Exception:
        pass
    return {"unloaded": True}


@router.post("/unload/{model_id:path}")
async def unload_model(model_id: str):
    """Unloads a model from memory so its files can be safely deleted."""
    try:
        gen = generator_registry.get_generator(model_id)
    except ValueError as exc:
        if model_id in generator_registry._generators:
            raise HTTPException(409, str(exc)) from exc
        return {"unloaded": True}  # No runtime registered for these files.
    # unload() on ExtensionProcess deliberately swallows IPC errors; deletion
    # needs a confirmed process exit so no worker can retain file handles.
    if isinstance(gen, ExtensionProcess):
        gen.stop()
    else:
        gen.unload()
        if gen.is_loaded():
            raise HTTPException(409, "Model is still loaded; weights were preserved")
    return {"unloaded": True}


@router.post("/hf-download/pause")
async def pause_hf_download(model_id: str):
    control = _download_control(model_id)
    control["pause"].set()
    return {"paused": True}


@router.post("/hf-download/cancel")
async def cancel_hf_download(model_id: str):
    control = _download_control(model_id)
    control["cancel"].set()
    return {"cancelled": True}


@router.post("/hf-download-sources")
async def hf_download_sources(request: FastAPIRequest, model_id: str):
    """Download sources into one validated node or extension-shared target."""
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be an object")
        raw_sources = body.get("sources")
        if raw_sources is None:
            raise ValueError("sources are required")
        sources = normalize_model_sources({"model_sources": raw_sources})
        model_root = resolve_weight_storage_root(registry_module.MODELS_DIR, model_id)
        destinations = {
            source["id"]: resolve_source_destination_at_root(
                model_root, source["destination"]
            )
            for source in sources
        }
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc

    # Authorization authenticates the Modly server itself. Hugging Face has a
    # separate header so the two credentials can never be confused or logged.
    hf_token = (
        request.headers.get("x-hf-token")
        or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        or os.environ.get("HF_TOKEN")
        or None
    )
    control = _new_download_control(model_id)

    async def stream():
        loop = asyncio.get_running_loop()

        def _fmt(data: dict) -> str:
            return f"data: {json.dumps(data)}\n\n"

        try:
            yield _fmt({"percent": 0, "status": "Listing repository files..."})
            files_by_source: dict[str, list[str]] = {}

            for source in sources:
                _check_download_control(control)

                def _list_files(current=source):
                    from huggingface_hub import list_repo_files
                    listed = list_repo_files(
                        current["repo_id"],
                        revision=current.get("revision"),
                        token=hf_token,
                    )
                    include = current.get("include_prefixes", [])
                    skip = current.get("skip_prefixes", [])
                    return [
                        filename for filename in listed
                        if (not include or any(filename.startswith(prefix) for prefix in include))
                        if not any(filename.startswith(prefix) for prefix in skip)
                    ]

                files = await loop.run_in_executor(None, _list_files)
                if not files:
                    raise RuntimeError(
                        f'No files found in Hugging Face repo: {source["repo_id"]}'
                    )
                destination = destinations[source["id"]]
                for filename in files:
                    resolve_download_path(destination, filename)
                files_by_source[source["id"]] = files

            validate_source_file_plan(sources, files_by_source)
            planned_files = [
                (source, filename)
                for source in sources
                for filename in files_by_source[source["id"]]
            ]
            total = len(planned_files)
            yield _fmt({"percent": 1, "status": f"Downloading {total} files..."})

            from huggingface_hub import hf_hub_url

            for index, (source, filename) in enumerate(planned_files):
                _check_download_control(control)
                display_file = f'{source["id"]}/{filename}'
                base_percent = 1 + round(index / total * 94)
                yield _fmt({
                    "percent": base_percent,
                    "file": display_file,
                    "fileIndex": index + 1,
                    "totalFiles": total,
                    "status": f"Starting {display_file}",
                    "bytesDownloaded": 0,
                    "stalledSeconds": 0,
                })

                queue: asyncio.Queue[dict] = asyncio.Queue()

                def _progress(message: dict) -> None:
                    message["file"] = display_file
                    loop.call_soon_threadsafe(queue.put_nowait, message)

                url = hf_hub_url(
                    repo_id=source["repo_id"],
                    filename=filename,
                    revision=source.get("revision"),
                )
                future = loop.run_in_executor(
                    None,
                    lambda: _download_file_streamed(
                        url=url,
                        filename=filename,
                        dest_dir=str(destinations[source["id"]]),
                        file_index=index + 1,
                        total_files=total,
                        base_percent=base_percent,
                        progress_cb=_progress,
                        control=control,
                        token=hf_token,
                    ),
                )
                while not future.done():
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=2.0)
                    except asyncio.TimeoutError:
                        continue
                    yield _fmt(message)

                final_size = await future
                _check_download_control(control)
                yield _fmt({
                    "percent": 1 + round((index + 1) / total * 94),
                    "file": display_file,
                    "fileIndex": index + 1,
                    "totalFiles": total,
                    "status": "Downloaded",
                    "bytesDownloaded": final_size,
                    "stalledSeconds": 0,
                })

            yield _fmt({"percent": 100, "status": "done"})
        except DownloadPaused:
            yield _fmt({"paused": True, "status": "paused"})
        except DownloadCancelled:
            for part in model_root.rglob("*.part"):
                part.unlink(missing_ok=True)
            yield _fmt({"cancelled": True, "status": "cancelled"})
        except Exception as exc:
            yield _fmt({"error": str(exc)})
        finally:
            if _download_controls.get(model_id) is control:
                _download_controls.pop(model_id, None)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/hf-download")
async def hf_download(
    repo_id: str,
    model_id: str,
    skip_prefixes: Optional[str] = None,
    include_prefixes: Optional[str] = None,
    x_hf_token: Optional[str] = Header(default=None),
):
    """
    Streams a HuggingFace Hub model download via SSE.
    Downloads into MODELS_DIR / model_id applying the filtering
    declared in the extension manifest (hf_skip_prefixes / hf_include_prefixes).

    skip_prefixes:    JSON-encoded list of path prefixes to exclude.
    include_prefixes: JSON-encoded list of path prefixes to include (whitelist).
    X-HF-Token:       HuggingFace access token for gated repos (from Electron settings).
                      A HEADER, not a query param: uvicorn logs the full request
                      line to stdout, python-bridge.ts pipes that into runtime.log,
                      and `log:readAll` hands that file to the user for bug
                      reports — the token used to travel all the way there.
    All fall back to the extension's manifest / environment when not supplied.

    SSE format: data: {"percent": 0-100, "file": "...", "status": "..."}
    """
    token = x_hf_token
    import json as _json
    import os
    dest_dir  = str(registry_module.MODELS_DIR / model_id)
    # Prefer skip_prefixes passed directly from the client (authoritative, no registry dep)
    if skip_prefixes:
        try:
            skip_list = _json.loads(skip_prefixes)
        except Exception:
            skip_list = []
    else:
        try:
            skip_list = generator_registry.get_manifest(model_id).get("hf_skip_prefixes", [])
        except KeyError:
            skip_list = []

    if include_prefixes:
        try:
            include_list = _json.loads(include_prefixes)
        except Exception:
            include_list = []
    else:
        try:
            include_list = generator_registry.get_manifest(model_id).get("hf_include_prefixes", [])
        except KeyError:
            include_list = []

    # Token: explicit query param > env var > None
    hf_token = token or os.environ.get("HUGGING_FACE_HUB_TOKEN") or os.environ.get("HF_TOKEN") or None
    control = _new_download_control(model_id)

    async def stream():
        loop = asyncio.get_running_loop()

        def _fmt(data: dict) -> str:
            return f"data: {json.dumps(data)}\n\n"

        try:
            yield _fmt({"percent": 0, "status": "Listing repository files..."})
            _check_download_control(control)

            def _list_files():
                from huggingface_hub import list_repo_files
                return [
                    f for f in list_repo_files(repo_id, token=hf_token)
                    if (not include_list or any(f.startswith(p) for p in include_list))
                    if not any(f.startswith(p) for p in skip_list)
                ]

            files = await loop.run_in_executor(None, _list_files)
            total = len(files)

            if total == 0:
                yield _fmt({"error": f"No files found in HuggingFace repo: {repo_id}"})
                return

            yield _fmt({"percent": 1, "status": f"Downloading {total} files..."})

            from huggingface_hub import hf_hub_url

            for i, filename in enumerate(files):
                _check_download_control(control)
                yield _fmt({
                    "percent": 1 + round(i / total * 94),
                    "file": filename,
                    "fileIndex": i + 1,
                    "totalFiles": total,
                    "status": f"Starting {filename}",
                    "bytesDownloaded": 0,
                    "stalledSeconds": 0,
                })

                base_pct = 1 + round(i / total * 94)
                queue: asyncio.Queue[dict] = asyncio.Queue()

                def _progress(msg: dict) -> None:
                    loop.call_soon_threadsafe(queue.put_nowait, msg)

                url = hf_hub_url(repo_id=repo_id, filename=filename)
                dl_future = loop.run_in_executor(
                    None,
                    lambda: _download_file_streamed(
                        url=url,
                        filename=filename,
                        dest_dir=dest_dir,
                        file_index=i + 1,
                        total_files=total,
                        base_percent=base_pct,
                        progress_cb=_progress,
                        control=control,
                        token=hf_token,
                    ),
                )

                while not dl_future.done():
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=2.0)
                    except asyncio.TimeoutError:
                        continue
                    else:
                        yield _fmt(msg)

                final_size = await dl_future
                _check_download_control(control)

                # Reserve 1-95 for file downloads, leave 95-100 for finalisation
                pct = 1 + round((i + 1) / total * 94)
                yield _fmt({
                    "percent": pct,
                    "file": filename,
                    "fileIndex": i + 1,
                    "totalFiles": total,
                    "status": "Downloaded",
                    "bytesDownloaded": final_size,
                    "stalledSeconds": 0,
                })

            yield _fmt({"percent": 100, "status": "done"})

        except DownloadPaused:
            yield _fmt({"paused": True, "status": "paused"})
        except DownloadCancelled:
            # Remove only partial files; completed files are preserved so the
            # next download can resume from where it left off.
            for part in Path(dest_dir).rglob("*.part"):
                part.unlink(missing_ok=True)
            yield _fmt({"cancelled": True, "status": "cancelled"})
        except Exception as exc:
            yield _fmt({"error": str(exc)})
        finally:
            # Only remove the control if it still belongs to this session.
            if _download_controls.get(model_id) is control:
                _download_controls.pop(model_id, None)

    return StreamingResponse(stream(), media_type="text/event-stream")


def _download_file_streamed(
    *,
    url: str,
    filename: str,
    dest_dir: str,
    file_index: int,
    total_files: int,
    base_percent: int,
    progress_cb,
    control: dict[str, threading.Event],
    token: Optional[str] = None,
) -> int:
    final_path = Path(dest_dir) / filename
    temp_path = final_path.with_suffix(final_path.suffix + ".part")
    final_path.parent.mkdir(parents=True, exist_ok=True)

    if final_path.exists():
        if not final_path.is_file():
            raise RuntimeError(f"Download target is not a regular file: {filename}")
        existing_size = final_path.stat().st_size
        if existing_size > 0:
            return existing_size
        final_path.unlink()

    # Explicit token (from caller) > env vars > none
    hf_token = (
        token
        or os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGINGFACE_HUB_TOKEN")
        or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    )
    headers = {"User-Agent": "modly/0.3.1"}
    if hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"

    retries = 3
    backoff = 2.0
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            _check_download_control(control)
            existing_bytes = temp_path.stat().st_size if temp_path.exists() else 0
            request_headers = dict(headers)
            request_url = url
            if existing_bytes > 0:
                request_url = _resolve_direct_download_url(url, headers)
                request_headers["Range"] = f"bytes={existing_bytes}-"

            request = Request(request_url, headers=request_headers)
            with urlopen(request, timeout=30) as response:
                resumed = existing_bytes > 0 and getattr(response, "status", None) == 206
                if existing_bytes > 0 and not resumed:
                    temp_path.unlink(missing_ok=True)
                    existing_bytes = 0

                total_bytes = _response_total_bytes(response.headers, existing_bytes if resumed else 0)
                bytes_downloaded = existing_bytes
                last_emit = 0.0
                chunk_size = 1024 * 1024
                mode = "ab" if resumed else "wb"

                progress_cb({
                    "percent": base_percent,
                    "file": filename,
                    "fileIndex": file_index,
                    "totalFiles": total_files,
                    "status": _download_status(bytes_downloaded, total_bytes, attempt, retries, resumed=resumed),
                    "bytesDownloaded": bytes_downloaded,
                    "totalBytes": total_bytes,
                    "stalledSeconds": 0,
                })

                with open(temp_path, mode) as out:
                    while True:
                        _check_download_control(control)
                        try:
                            chunk = response.read(chunk_size)
                        except socket.timeout as exc:
                            raise TimeoutError(f"Timed out while downloading {filename}") from exc

                        if not chunk:
                            break

                        out.write(chunk)
                        bytes_downloaded += len(chunk)

                        now = time.monotonic()
                        if now - last_emit >= 0.5:
                            progress_cb({
                                "percent": base_percent,
                                "file": filename,
                                "fileIndex": file_index,
                                "totalFiles": total_files,
                                "status": _download_status(bytes_downloaded, total_bytes, attempt, retries, resumed=resumed),
                                "bytesDownloaded": bytes_downloaded,
                                "totalBytes": total_bytes,
                                "stalledSeconds": 0,
                            })
                            last_emit = now

            temp_path.replace(final_path)
            return bytes_downloaded

        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            preserved_bytes = temp_path.stat().st_size if temp_path.exists() else 0
            progress_cb({
                "percent": base_percent,
                "file": filename,
                "fileIndex": file_index,
                "totalFiles": total_files,
                "status": f"Retrying after error ({attempt}/{retries})…",
                "bytesDownloaded": preserved_bytes,
                "stalledSeconds": 0,
            })
            if attempt >= retries:
                break
            time.sleep(backoff)
            backoff *= 2

    raise RuntimeError(f"Failed to download {filename}: {last_error}")


def _resolve_direct_download_url(url: str, headers: dict[str, str]) -> str:
    # HEAD request: follows redirects to get the final CDN URL without downloading the body.
    request = Request(url, headers=headers, method="HEAD")
    with urlopen(request, timeout=30) as response:
        return response.geturl()


def _parse_content_length(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _download_status(downloaded: int, total: Optional[int], attempt: int, retries: int, resumed: bool = False) -> str:
    prefix = "Resuming…" if resumed and downloaded > 0 else "Downloading…"
    if total and total > 0:
        pct = min(100, round(downloaded / total * 100))
        return f"{prefix} {pct}%"
    if retries > 1 and attempt > 1:
        return f"{prefix} retry {attempt}/{retries}"
    return prefix


def _response_total_bytes(headers, already_downloaded: int) -> Optional[int]:
    content_range = headers.get("Content-Range")
    if content_range and "/" in content_range:
        total_raw = content_range.split("/")[-1].strip()
        try:
            return int(total_raw)
        except (TypeError, ValueError):
            pass

    content_length = _parse_content_length(headers.get("Content-Length"))
    if content_length is None:
        return None
    return already_downloaded + content_length
