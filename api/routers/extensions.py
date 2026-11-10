import asyncio
import json
import os
import platform as platform_module
import re
import subprocess
import sys
import shutil
import logging
import traceback
import time
import uuid
from pathlib import Path
import httpx
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from services.builtin_extensions import BUILTIN_MARKER
from services.extension_runtime import (
    container_api_url,
    docker_capabilities,
    docker_run_prefix,
    extension_is_visible,
    extension_runtime,
    remove_extension_image,
    runtime_settings,
    set_docker_enabled,
)
from services.model_sources import (
    normalize_model_sources,
    normalize_weight_group_references,
    normalize_weight_groups,
    normalize_weight_variants,
)

router = APIRouter(tags=["extensions"])
_install_tasks: set[asyncio.Task] = set()
_install_logger = logging.getLogger("modly.extensions.install")
_install_jobs: dict[str, dict[str, object]] = {}
_active_process_runs: dict[str, tuple[str, subprocess.Popen, str | None]] = {}
_INSTALL_JOB_TTL_SECONDS = 60 * 60
_OFFICIAL_REGISTRY_URL = os.environ.get(
    "MODLY_OFFICIAL_EXTENSION_REGISTRY",
    "https://raw.githubusercontent.com/lightningpixel/modly-official-extension/main/registry.json",
)
_OFFICIAL_REGISTRY_TTL_SECONDS = 5 * 60
_official_registry_cache: tuple[float, set[str]] | None = None

_SAFE_EXTENSION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _extension_root() -> Path:
    from services.generator_registry import EXTENSIONS_DIR
    if EXTENSIONS_DIR is None:
        raise HTTPException(400, "EXTENSIONS_DIR not configured")
    EXTENSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return EXTENSIONS_DIR.resolve()


def _extension_path(extension_id: str) -> Path:
    if not _SAFE_EXTENSION_ID.fullmatch(extension_id):
        raise HTTPException(400, "Invalid extension id")
    root = _extension_root()
    # Keep the lexical destination. Developer-mode extensions are symlinks;
    # resolving here would incorrectly reject them as escaping the extension
    # root and could make uninstall target their source checkout.
    path = root / extension_id
    if path.parent != root:
        raise HTTPException(400, "Invalid extension path")
    return path


def _canonical_source(value: object) -> str:
    return str(value).strip().lower().rstrip("/") if isinstance(value, str) else ""


async def _trusted_repositories() -> set[str]:
    global _official_registry_cache
    now = time.monotonic()
    if _official_registry_cache and now - _official_registry_cache[0] < _OFFICIAL_REGISTRY_TTL_SECONDS:
        return _official_registry_cache[1]
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
            response = await client.get(
                _OFFICIAL_REGISTRY_URL,
                headers={"User-Agent": "Modly-Server"},
            )
            response.raise_for_status()
            payload = response.json()
        repositories = {
            _canonical_source(item)
            for item in payload.get("trusted_repos", [])
            if _canonical_source(item)
        } if isinstance(payload, dict) else set()
        _official_registry_cache = (now, repositories)
        return repositories
    except Exception:
        # An offline server should still list extensions. Preserve the last
        # successful answer when possible, exactly as the desktop cache did.
        if _official_registry_cache is None:
            _official_registry_cache = (now, set())
        return _official_registry_cache[1]


def _node_for_client(
    node: dict,
    *,
    manifest_params_schema: object,
    manifest_param_defaults: object,
    weight_groups: list[dict] | None,
) -> dict:
    params_schema = node.get("params_schema", manifest_params_schema)
    variants = normalize_weight_variants(node, params_schema)
    if isinstance(variants, dict):
        variants = {
            "param": variants.get("param"),
            "default": variants.get("default"),
            "options": [
                {
                    "id": option.get("id"),
                    "label": option.get("label", option.get("id")),
                    "sizeGb": option.get("size_gb"),
                    "vramGb": option.get("vram_gb"),
                }
                for option in variants.get("options", [])
                if isinstance(option, dict)
            ],
        }
    return {
        "id": node.get("id", ""),
        "name": node.get("name", node.get("id", "")),
        "input": node.get("input", "image"),
        "inputs": node.get("inputs"),
        "inputLabels": node.get("input_labels"),
        "output": node.get("output", "mesh"),
        "paramsSchema": params_schema if isinstance(params_schema, list) else [],
        "paramDefaults": {
            **(manifest_param_defaults if isinstance(manifest_param_defaults, dict) else {}),
            **(node.get("param_defaults") if isinstance(node.get("param_defaults"), dict) else {}),
        },
        "hfRepo": node.get("hf_repo"),
        "downloadCheck": node.get("download_check"),
        "hfSkipPrefixes": node.get("hf_skip_prefixes"),
        "hfIncludePrefixes": node.get("hf_include_prefixes"),
        "hasModelSources": normalize_model_sources(node) is not None,
        "weightGroups": normalize_weight_group_references(
            node,
            weight_groups,
            field_name=f'nodes[{node.get("id", "")}].weight_groups',
        ),
        "weightVariants": variants,
    }


def _manifest_for_client(
    manifest: dict,
    *,
    corrupted: bool = False,
    runtime: str | None = None,
    trusted_repos: set[str] | None = None,
) -> dict:
    weight_groups = normalize_weight_groups(manifest)
    nodes = [
        _node_for_client(
            node,
            manifest_params_schema=manifest.get("params_schema", []),
            manifest_param_defaults=manifest.get("param_defaults", {}),
            weight_groups=weight_groups,
        )
        for node in manifest.get("nodes", [])
        if isinstance(node, dict) and node.get("id")
    ]
    groups = []
    for group in weight_groups or []:
        if not isinstance(group, dict) or not group.get("id"):
            continue
        dependent = [
            node["id"] for node in nodes
            if node.get("weightGroups") and group["id"] in node["weightGroups"]
        ]
        groups.append({"id": group["id"], "dependentNodeIds": dependent})
    return {
        "type": manifest.get("type", "model"),
        "id": manifest.get("id", ""),
        "name": manifest.get("displayName", manifest.get("name", manifest.get("id", ""))),
        "version": manifest.get("version"),
        "description": manifest.get("description"),
        "author": (
            manifest.get("author", {}).get("name")
            if isinstance(manifest.get("author"), dict)
            else manifest.get("author")
        ),
        "source": manifest.get("source"),
        "entry": manifest.get("entry", ""),
        "trusted": bool(manifest.get("builtin", False)) or _canonical_source(manifest.get("source")) in (trusted_repos or set()),
        "builtin": bool(manifest.get("builtin", False)),
        "nodes": nodes,
        "weightGroups": groups or None,
        "corrupted": corrupted,
        "runtime": runtime,
    }


@router.get("/list")
async def list_extensions():
    result = []
    root = _extension_root()
    trusted_repos = await _trusted_repositories()
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        if not extension_is_visible(root, path):
            continue
        manifest_path = path / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest must be an object")
            runtime_info = extension_runtime(path)
            if isinstance(runtime_info.get("localSource"), str):
                manifest = {**manifest, "source": f'local://{runtime_info["localSource"]}'}
            result.append(_manifest_for_client(
                manifest,
                runtime=runtime_info.get("kind"),
                trusted_repos=trusted_repos,
            ))
        except Exception:
            result.append({
                "type": "model", "id": path.name, "name": path.name,
                "trusted": False, "builtin": False, "nodes": [],
                "corrupted": True,
                "manifestError": "invalid" if manifest_path.exists() else "missing",
            })
    return result


class RuntimeSettings(BaseModel):
    useDocker: bool


@router.get("/runtime")
async def get_runtime_settings():
    return await asyncio.to_thread(docker_capabilities, _extension_root())


@router.put("/runtime")
async def update_runtime_settings(body: RuntimeSettings):
    root = _extension_root()
    if body.useDocker:
        capabilities = await asyncio.to_thread(docker_capabilities, root)
        if not capabilities["dockerAvailable"]:
            raise HTTPException(409, capabilities["reason"] or "Docker is not available")
    set_docker_enabled(root, body.useDocker)
    await _stop_process_runs()
    from services.generator_registry import generator_registry
    await asyncio.to_thread(generator_registry.reload)
    return await asyncio.to_thread(docker_capabilities, root)


class GitHubInstall(BaseModel):
    url: str


class LocalInstall(BaseModel):
    path: str


class ProcessRun(BaseModel):
    extension_id: str
    input: dict = Field(default_factory=dict)
    params: dict = Field(default_factory=dict)


@router.post("/install-local")
async def install_local(body: LocalInstall):
    """Link a developer checkout through the desktop-managed local server.

    Standalone/remote servers deliberately reject host paths; those clients
    must use a fetchable URL or an upload flow instead of naming arbitrary
    files on the server.
    """
    if os.environ.get("MODLY_DESKTOP_MANAGED") != "1":
        raise HTTPException(403, "Local-folder linking is available only on the bundled server")
    root = _extension_root()
    if runtime_settings(root)["useDocker"]:
        raise HTTPException(409, "Local-folder extensions are unavailable in Docker mode")
    source = Path(body.path).expanduser().resolve()
    if not source.is_dir():
        raise HTTPException(400, "The selected local extension folder does not exist")

    from services.extension_installer import _begin_registration, validate_manifest
    from services.extension_runtime import write_extension_runtime
    from services.generator_registry import generator_registry

    manifest, _, _, _ = validate_manifest(source)
    extension_id = manifest["id"]
    destination = _extension_path(extension_id)
    if destination == source:
        raise HTTPException(400, "The local source folder is already the extension destination")
    if (destination / BUILTIN_MARKER).is_file():
        raise HTTPException(409, "Built-in extensions are managed by the server")
    if destination.exists():
        current, _, current_process, _ = validate_manifest(destination)
        incoming_process = manifest.get("type") == "process"
        if current["id"] != extension_id or current_process != incoming_process:
            raise HTTPException(409, "The existing extension cannot be safely replaced")

    backup = root / f".modly-backup-{extension_id}-{uuid.uuid4().hex}"
    registration_state = None
    had_previous = destination.exists() or destination.is_symlink()
    await asyncio.to_thread(generator_registry.reload)
    try:
        if had_previous:
            destination.rename(backup)
        destination.symlink_to(source, target_is_directory=True)
        write_extension_runtime(
            destination,
            {"kind": "system", "localSource": str(source)},
        )
        validation_capability, registration_state = _begin_registration(root, extension_id)
        await asyncio.to_thread(generator_registry.reload, validation_capability)
        relevant = {
            key: value for key, value in generator_registry.load_errors().items()
            if key == extension_id or key.startswith(f"{extension_id}/")
        }
        if relevant:
            if had_previous:
                destination.unlink(missing_ok=True)
                backup.rename(destination)
                await asyncio.to_thread(generator_registry.reload)
                raise RuntimeError(
                    "Local extension validation failed; the previous version was restored. "
                    + "; ".join(relevant.values())
                )
            # A fresh developer checkout remains linked so Repair can create
            # its environment, matching the previous local-development flow.
            return {
                "success": False,
                "needsRepair": True,
                "error": "The local extension is linked but needs Repair: " + "; ".join(relevant.values()),
                "extensionId": extension_id,
                "extension": _manifest_for_client(
                    {**manifest, "source": f"local://{source}"},
                    runtime="system",
                ),
                "localPath": str(source),
            }
        if backup.exists() or backup.is_symlink():
            if backup.is_symlink():
                backup.unlink()
            else:
                shutil.rmtree(backup)
        return {
            "success": True,
            "extensionId": extension_id,
            "extension": _manifest_for_client(
                {**manifest, "source": f"local://{source}"},
                runtime="system",
            ),
            "localPath": str(source),
        }
    except Exception as exc:
        if destination.is_symlink():
            destination.unlink(missing_ok=True)
        if had_previous and (backup.exists() or backup.is_symlink()) and not destination.exists():
            backup.rename(destination)
        await asyncio.to_thread(generator_registry.reload)
        raise HTTPException(500, str(exc)) from exc
    finally:
        if registration_state is not None:
            registration_state.unlink(missing_ok=True)


def _prune_install_jobs() -> None:
    cutoff = time.monotonic() - _INSTALL_JOB_TTL_SECONDS
    expired = [
        install_id
        for install_id, job in _install_jobs.items()
        if job.get("terminal") is not None and float(job.get("updated_at", 0)) < cutoff
    ]
    for install_id in expired:
        _install_jobs.pop(install_id, None)


async def _stop_process_runs(extension_id: str | None = None) -> None:
    selected = [
        (run_id, process, container_name)
        for run_id, (owner, process, container_name) in list(_active_process_runs.items())
        if extension_id is None or owner == extension_id
    ]
    for run_id, process, container_name in selected:
        if process.poll() is None:
            process.kill()
        if container_name:
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    [shutil.which("docker") or "docker", "rm", "-f", container_name],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        _active_process_runs.pop(run_id, None)


@router.post("/run-process")
async def run_process(body: ProcessRun):
    extension_dir = _extension_path(body.extension_id)
    if not extension_is_visible(_extension_root(), extension_dir):
        raise HTTPException(404, "Extension is not installed for the selected runtime")
    manifest_path = extension_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(400, "Extension manifest is unreadable") from exc
    if manifest.get("type") != "process":
        raise HTTPException(400, "Extension is not a process extension")
    entry = manifest.get("entry")
    if not isinstance(entry, str) or not entry:
        raise HTTPException(400, "Process extension has no entry")
    runtime = extension_runtime(extension_dir)
    is_docker = runtime.get("kind") == "docker"
    runtime_entry = runtime.get("entry", entry) if is_docker else entry
    if not isinstance(runtime_entry, str) or not runtime_entry:
        raise HTTPException(500, "Extension runtime entry metadata is incomplete")
    if Path(runtime_entry).is_absolute() or ".." in Path(runtime_entry).parts:
        raise HTTPException(400, "Process extension runtime entry is unsafe")
    entry_path = (extension_dir / entry).resolve()
    if extension_dir.resolve() not in entry_path.parents or not entry_path.is_file():
        raise HTTPException(400, "Process extension entry is missing or unsafe")

    import services.generator_registry as registry
    workspace = registry.WORKSPACE_DIR
    temp_dir = workspace / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    def container_value(value):
        if isinstance(value, dict):
            return {key: container_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [container_value(item) for item in value]
        if not isinstance(value, str) or not Path(value).is_absolute():
            return value
        try:
            path = Path(value).resolve()
            for host_root, container_root in (
                (workspace.resolve(), Path("/modly/workspace")),
                (registry.MODELS_DIR.resolve(), Path("/modly/models")),
            ):
                try:
                    return str(container_root / path.relative_to(host_root))
                except ValueError:
                    continue
        except OSError:
            pass
        return value

    run_input = container_value(body.input) if is_docker else body.input
    run_params = container_value(body.params) if is_docker else body.params
    runtime_workspace = "/modly/workspace" if is_docker else str(workspace)
    runtime_temp = "/modly/workspace/tmp" if is_docker else str(temp_dir)
    payload = json.dumps({
        "input": run_input,
        "params": run_params,
        "nodeId": body.input.get("nodeId", ""),
        "workspaceDir": runtime_workspace,
        "tempDir": runtime_temp,
    }) + "\n"
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "MODLY_API_DIR": str(Path(__file__).resolve().parents[1]),
        "MODLY_NODE_EXECUTABLE": os.environ.get("MODLY_NODE_EXECUTABLE", "node"),
        "EXTENSION_DIR": str(extension_dir),
        "WORKSPACE_DIR": str(workspace),
        "TEMP_DIR": str(temp_dir),
    }
    container_name: str | None = None
    if is_docker:
        capabilities = await asyncio.to_thread(docker_capabilities, _extension_root())
        if not capabilities["dockerAvailable"]:
            raise HTTPException(503, capabilities["reason"] or "Docker is unavailable")
        image = runtime.get("image")
        if not isinstance(image, str) or not image:
            raise HTTPException(500, "Extension Docker runtime metadata is incomplete")
        container_name = f"modly-process-{body.extension_id}-{uuid.uuid4().hex[:10]}"
        command = docker_run_prefix(
            image,
            name=container_name,
            models_dir=registry.MODELS_DIR,
            workspace_dir=workspace,
            api_dir=Path(__file__).resolve().parents[1],
            cache_dir=registry.MODELS_DIR / ".modly-runtime-cache" / body.extension_id,
            nvidia_mode=(
                str(capabilities.get("nvidiaContainerMode", ""))
                if runtime.get("nvidia") and capabilities["nvidiaToolkitAvailable"]
                else ""
            ),
            environment={
                "EXTENSION_DIR": "/opt/modly/extension",
                "WORKSPACE_DIR": "/modly/workspace",
                "TEMP_DIR": "/modly/workspace/tmp",
                "MODLY_API_DIR": "/modly/api",
                "MODLY_API_URL": container_api_url(),
                **({"MODLY_SERVER_TOKEN": None}
                   if os.environ.get("MODLY_SERVER_TOKEN") else {}),
                **({"HF_TOKEN": None} if os.environ.get("HF_TOKEN") else {}),
                **({"HUGGING_FACE_HUB_TOKEN": None}
                   if os.environ.get("HUGGING_FACE_HUB_TOKEN") else {}),
            },
        )
        if Path(runtime_entry).suffix == ".py":
            command += ["/opt/modly/runtime-python", f"/opt/modly/extension/{runtime_entry}"]
        else:
            command += ["node", "/modly/api/process_runner.cjs", "/opt/modly/extension", runtime_entry]
    elif entry_path.suffix == ".py":
        if sys.platform == "win32":
            venv_python = extension_dir / ".venv" / "Scripts" / "python.exe"
        else:
            venv_python = extension_dir / ".venv" / "bin" / "python"
        executable = str(venv_python if venv_python.is_file() else Path(sys.executable))
        command = [executable, str(entry_path)]
    else:
        node = os.environ.get("MODLY_NODE_EXECUTABLE", "node")
        command = [node, str(Path(__file__).resolve().parents[1] / "process_runner.cjs"), str(extension_dir), entry]
    run_id = uuid.uuid4().hex
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    _active_process_runs[run_id] = (body.extension_id, process, container_name)
    try:
        stdout, stderr = await asyncio.to_thread(
            process.communicate, payload, timeout=60 * 60
        )
    except subprocess.TimeoutExpired as exc:
        process.kill()
        await asyncio.to_thread(process.communicate)
        raise HTTPException(504, "Extension process timed out") from exc
    finally:
        _active_process_runs.pop(run_id, None)
    messages = []
    for line in stdout.splitlines():
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    done = next((message for message in reversed(messages) if message.get("type") == "done"), None)
    error = next((message for message in reversed(messages) if message.get("type") == "error"), None)
    if done is not None:
        output = done.get("result") or {}
        def host_value(value):
            if isinstance(value, dict):
                return {key: host_value(item) for key, item in value.items()}
            if isinstance(value, list):
                return [host_value(item) for item in value]
            if isinstance(value, str):
                for container_root, host_root in (
                    ("/modly/workspace", workspace),
                    ("/modly/models", registry.MODELS_DIR),
                ):
                    if value == container_root or value.startswith(container_root + "/"):
                        return str(host_root / Path(value).relative_to(container_root))
            return value

        if is_docker:
            output = host_value(output)
        return {"success": True, "result": output}
    message = error.get("message") if error else (stderr.strip() or f"Process exited with {process.returncode}")
    raise HTTPException(500, message)


@router.post("/install-github")
async def install_from_github(body: GitHubInstall):
    """Fetch and install on the server, returning newline-delimited SSE progress."""
    from services.extension_installer import install_github

    _prune_install_jobs()
    install_id = uuid.uuid4().hex
    job: dict[str, object] = {
        "latest": None,
        "terminal": None,
        "updated_at": time.monotonic(),
    }
    _install_jobs[install_id] = job

    async def events():
        queue: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=100)
        connected = True

        async def emit(event: dict) -> None:
            job["latest"] = event
            job["updated_at"] = time.monotonic()
            if event.get("step") in ("result", "error"):
                job["terminal"] = event
            if not connected:
                return
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)

        async def run() -> None:
            try:
                result = await install_github(body.url, _extension_root(), emit)
                trusted_repos = await _trusted_repositories()
                # The done progress event is emitted by the installer. Send the
                # full normalized extension as the terminal result.
                await emit({
                    "step": "result",
                    **result,
                    "extension": _manifest_for_client(
                        result["manifest"],
                        runtime=extension_runtime(_extension_path(result["extensionId"])).get("kind"),
                        trusted_repos=trusted_repos,
                    ),
                })
            except Exception as exc:
                _install_logger.exception("Extension installation failed")
                details = getattr(exc, "details", "") or traceback.format_exc()
                await emit({"step": "error", "message": str(exc), "details": details})
            finally:
                if connected:
                    if queue.full():
                        queue.get_nowait()
                    queue.put_nowait(None)

        task = asyncio.create_task(run())
        _install_tasks.add(task)
        task.add_done_callback(_install_tasks.discard)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    # Keep reverse proxies from buffering or retiring an idle
                    # install while a setup command is temporarily silent.
                    yield ": keep-alive\n\n"
                    continue
                if event is None:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            # Installation is server-owned and continues if the desktop closes
            # the progress stream. Startup recovery covers a server shutdown.
            connected = False

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Modly-Install-Id": install_id,
        },
    )


@router.get("/install-github/{install_id}/status")
async def install_github_status(install_id: str):
    """Return recoverable progress when an install's SSE connection is lost."""
    _prune_install_jobs()
    job = _install_jobs.get(install_id)
    if job is None:
        raise HTTPException(404, "Extension installation is no longer available")
    terminal = job.get("terminal")
    return {
        "done": terminal is not None,
        "latest": job.get("latest"),
        "terminal": terminal,
    }


@router.delete("/{extension_id}")
async def uninstall_extension(extension_id: str):
    path = _extension_path(extension_id)
    from services.builtin_extensions import BUILTIN_MARKER
    if (path / BUILTIN_MARKER).is_file():
        raise HTTPException(403, "Built-in extensions are managed by the server")
    runtime = extension_runtime(path)
    await _stop_process_runs(extension_id)
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
    from services.generator_registry import generator_registry
    try:
        await asyncio.to_thread(generator_registry.reload)
    finally:
        await asyncio.to_thread(remove_extension_image, runtime)
    return {"success": True}


@router.post("/reload")
async def reload_extensions(payload: dict | None = Body(default=None)):
    """
    Re-scans the extensions/ folder and reloads the registry without restarting FastAPI.
    Unloads all currently loaded generators before reloading.
    """
    from services.generator_registry import generator_registry
    await _stop_process_runs()
    validation_capability = None
    if isinstance(payload, dict):
        candidate = payload.get("validationCapability")
        if isinstance(candidate, dict):
            validation_capability = candidate
    # Off the event loop: reload waits for any in-progress model load.
    await asyncio.to_thread(generator_registry.reload, validation_capability)
    return {
        "reloaded": True,
        "models":   list(generator_registry._generators.keys()),
        "errors":   generator_registry.load_errors(),
    }


@router.post("/setup/{ext_id}")
async def setup_extension(ext_id: str):
    """Repair and validate an extension entirely on the selected server."""
    from services.extension_installer import (
        _begin_registration,
        _setup_docker_extension,
        _setup_extension,
        validate_manifest,
    )
    from services.generator_registry import generator_registry

    ext_dir = _extension_path(ext_id)
    if not ext_dir.exists():
        raise HTTPException(404, f"Extension '{ext_id}' is not installed")
    manifest, entry, _, _ = validate_manifest(ext_dir)
    previous_runtime = extension_runtime(ext_dir)
    repaired_runtime = previous_runtime
    registration_state = None

    async def log_progress(event: dict) -> None:
        message = event.get("message")
        if isinstance(message, str) and message:
            _install_logger.info("[%s repair] %s", ext_id, message)

    # Stop any process/container using the extension before mutating its
    # environment. reload() records broken extensions as load errors rather
    # than failing the entire server.
    await _stop_process_runs(ext_id)
    await asyncio.to_thread(generator_registry.reload)
    try:
        if previous_runtime.get("kind") == "docker":
            repaired_runtime = await _setup_docker_extension(
                _extension_root(), ext_dir, manifest, entry, log_progress
            )
        else:
            await _setup_extension(ext_dir, manifest, entry, log_progress)
            repaired_runtime = {"kind": "system"}
            from services.extension_runtime import write_extension_runtime
            write_extension_runtime(ext_dir, repaired_runtime)

        validation_capability, registration_state = _begin_registration(
            _extension_root(), ext_id
        )
        await asyncio.to_thread(generator_registry.reload, validation_capability)
        relevant = {
            key: value for key, value in generator_registry.load_errors().items()
            if key == ext_id or key.startswith(f"{ext_id}/")
        }
        if relevant:
            raise RuntimeError(
                "Extension runtime validation failed: " + "; ".join(relevant.values())
            )
    except Exception as exc:
        if repaired_runtime.get("image") != previous_runtime.get("image"):
            from services.extension_runtime import write_extension_runtime
            write_extension_runtime(ext_dir, previous_runtime)
            await asyncio.to_thread(remove_extension_image, repaired_runtime)
        if registration_state is not None:
            registration_state.unlink(missing_ok=True)
        await asyncio.to_thread(generator_registry.reload)
        detail = getattr(exc, "details", "") or str(exc)
        raise HTTPException(500, detail) from exc

    if registration_state is not None:
        registration_state.unlink(missing_ok=True)
    if repaired_runtime.get("image") != previous_runtime.get("image"):
        await asyncio.to_thread(remove_extension_image, previous_runtime)
    return {"status": "ok", "runtime": repaired_runtime}


@router.get("/errors")
async def extension_errors():
    """Returns extension loading errors (invalid manifest, failed import, etc.)."""
    from services.generator_registry import generator_registry
    return generator_registry.load_errors()


def _detect_nvidia_gpu() -> tuple[int, int]:
    """
    Returns (compute capability, max CUDA version) — e.g. (86, 124) — or (0, 0)
    when there is no NVIDIA GPU.

    Asks nvidia-smi rather than torch: this process runs in Modly's main venv,
    which has no torch at all (see api/requirements.txt), so a torch import
    always failed here and silently reported every machine as CPU-only. Asking
    the driver also sidesteps the ROCm ambiguity — PyTorch's HIP build answers
    the whole torch.cuda API, reporting (12, 0) for a gfx1200 Radeon exactly
    like an sm_120 Blackwell. Mirrors parseNvidiaSmi in
    electron/main/gpu-detect.ts, including the driver → CUDA version table.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=compute_cap,driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return 0, 0
    if result.returncode != 0:
        return 0, 0

    line = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
    if not line:
        return 0, 0
    parts = [part.strip() for part in line.split(",")]

    try:
        sm = round(float(parts[0]) * 10)
    except (ValueError, IndexError):
        sm = 86
    try:
        driver_major = int((parts[1] if len(parts) > 1 else "0").split(".")[0])
    except ValueError:
        driver_major = 0

    cuda_version = 118  # safe minimum
    for threshold, version in (
        (570, 128), (560, 126), (555, 125), (550, 124),
        (545, 123), (535, 122), (530, 121), (525, 120), (520, 118),
    ):
        if driver_major >= threshold:
            cuda_version = version
            break
    return sm, cuda_version


def _rocm_index_url() -> str:
    """The pip index a ROCm torch install must come from. Mirrors
    resolveRocmTorchSpec in electron/main/gpu-detect.ts."""
    override = os.environ.get("MODLY_ROCM_INDEX", "").strip()
    if override:
        return override
    if sys.platform == "win32":
        return "https://repo.amd.com/rocm/whl-multi-arch/"
    return "https://download.pytorch.org/whl/rocm7.2"


def _node_arch() -> str:
    """platform.machine() mapped onto Node's process.arch vocabulary, so
    setup.py sees the same values whichever side launched it."""
    machine = platform_module.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return machine


def _detect_gfx_target() -> str:
    """
    Returns the ROCm compute target (e.g. "gfx1200"), or "" when there is no AMD GPU.

    Reads the kernel's KFD topology rather than asking torch: this process runs
    in Modly's main venv, which has no torch at all (see api/requirements.txt).
    The amdgpu driver publishes the target on its own, so no ROCm install is
    needed either. Mirrors electron/main/gpu-detect.ts.
    """
    kfd_nodes = Path("/sys/class/kfd/kfd/topology/nodes")
    if not Path("/dev/kfd").exists() or not kfd_nodes.is_dir():
        return ""

    def _prop(text: str, key: str) -> int:
        match = re.search(rf"^{key}\s+(\d+)\s*$", text, re.M)
        return int(match.group(1)) if match else 0

    try:
        nodes = sorted(kfd_nodes.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 0)
    except OSError:
        return ""

    # Among GPU nodes the largest simd_count wins, mirroring parseKfdGfxTarget
    # in electron/main/gpu-detect.ts: on an APU + dGPU machine the APU commonly
    # gets the lower node number, and the discrete card has more SIMDs.
    best_target, best_simd = "", 0
    for node in nodes:
        try:
            text = (node / "properties").read_text()
        except OSError:
            continue
        # Node 0 is the CPU node (simd_count 0) and carries no compute target.
        simd_count = _prop(text, "simd_count")
        if simd_count <= 0:
            continue
        # major*10000 + minor*100 + step, minor and step read as hex digits.
        version = _prop(text, "gfx_target_version")
        if version <= 0:
            continue
        major, minor, step = version // 10000, (version % 10000) // 100, version % 100
        if major <= 0 or minor > 15 or step > 15:
            continue
        if simd_count > best_simd:
            best_target, best_simd = f"gfx{major}{minor:x}{step:x}", simd_count
    return best_target
