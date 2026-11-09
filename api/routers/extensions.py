import asyncio
import json
import os
import platform as platform_module
import re
import subprocess
import sys
import shutil
import tarfile
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse
import httpx
from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(tags=["extensions"])

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
    path = (root / extension_id).resolve()
    if path.parent != root:
        raise HTTPException(400, "Invalid extension path")
    return path


def _node_for_client(node: dict) -> dict:
    variants = node.get("weight_variants")
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
        "paramsSchema": node.get("params_schema", []),
        "paramDefaults": node.get("param_defaults"),
        "hfRepo": node.get("hf_repo"),
        "downloadCheck": node.get("download_check"),
        "hfSkipPrefixes": node.get("hf_skip_prefixes"),
        "hfIncludePrefixes": node.get("hf_include_prefixes"),
        "hasModelSources": isinstance(node.get("model_sources"), list),
        "weightGroups": node.get("weight_groups"),
        "weightVariants": variants,
    }


def _manifest_for_client(manifest: dict, *, corrupted: bool = False) -> dict:
    nodes = [
        _node_for_client(node)
        for node in manifest.get("nodes", [])
        if isinstance(node, dict) and node.get("id")
    ]
    groups = []
    for group in manifest.get("weight_groups", []) or []:
        if not isinstance(group, dict) or not group.get("id"):
            continue
        dependent = [
            node["id"] for node in manifest.get("nodes", [])
            if isinstance(node, dict) and group["id"] in (node.get("weight_groups") or [])
        ]
        groups.append({"id": group["id"], "dependentNodeIds": dependent})
    return {
        "type": manifest.get("type", "model"),
        "id": manifest.get("id", ""),
        "name": manifest.get("name", manifest.get("id", "")),
        "version": manifest.get("version"),
        "description": manifest.get("description"),
        "author": manifest.get("author"),
        "source": manifest.get("source"),
        "entry": manifest.get("entry", ""),
        "trusted": False,
        "builtin": bool(manifest.get("builtin", False)),
        "nodes": nodes,
        "weightGroups": groups or None,
        "corrupted": corrupted,
    }


@router.get("/list")
async def list_extensions():
    result = []
    for path in sorted(_extension_root().iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        manifest_path = path / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest must be an object")
            result.append(_manifest_for_client(manifest))
        except Exception:
            result.append({
                "type": "model", "id": path.name, "name": path.name,
                "trusted": False, "builtin": False, "nodes": [],
                "corrupted": True,
                "manifestError": "invalid" if manifest_path.exists() else "missing",
            })
    return result


class GitHubInstall(BaseModel):
    url: str


class ProcessRun(BaseModel):
    extension_id: str
    input: dict = Field(default_factory=dict)
    params: dict = Field(default_factory=dict)


@router.post("/run-process")
async def run_process(body: ProcessRun):
    extension_dir = _extension_path(body.extension_id)
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
    entry_path = (extension_dir / entry).resolve()
    if extension_dir.resolve() not in entry_path.parents or not entry_path.is_file():
        raise HTTPException(400, "Process extension entry is missing or unsafe")

    import services.generator_registry as registry
    workspace = registry.WORKSPACE_DIR
    temp_dir = workspace / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "input": body.input,
        "params": body.params,
        "nodeId": body.input.get("nodeId", ""),
        "workspaceDir": str(workspace),
        "tempDir": str(temp_dir),
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
    if entry_path.suffix == ".py":
        if sys.platform == "win32":
            venv_python = extension_dir / ".venv" / "Scripts" / "python.exe"
        else:
            venv_python = extension_dir / ".venv" / "bin" / "python"
        executable = str(venv_python if venv_python.is_file() else Path(sys.executable))
        command = [executable, str(entry_path)]
    else:
        node = os.environ.get("MODLY_NODE_EXECUTABLE", "node")
        command = [node, str(Path(__file__).resolve().parents[1] / "process_runner.cjs"), str(extension_dir), entry]
    result = await asyncio.to_thread(
        subprocess.run, command, input=payload, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=60 * 60,
    )
    messages = []
    for line in result.stdout.splitlines():
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    done = next((message for message in reversed(messages) if message.get("type") == "done"), None)
    error = next((message for message in reversed(messages) if message.get("type") == "error"), None)
    if done is not None:
        return {"success": True, "result": done.get("result") or {}}
    message = error.get("message") if error else (result.stderr.strip() or f"Process exited with {result.returncode}")
    raise HTTPException(500, message)


def _extract_github_tarball(payload: bytes, destination: Path) -> None:
    archive_path = destination.parent / f"{destination.name}.tar.gz"
    archive_path.write_bytes(payload)
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            roots = {member.name.split("/", 1)[0] for member in members if member.name}
            if len(roots) != 1:
                raise ValueError("GitHub archive has an unexpected layout")
            root_name = next(iter(roots))
            for member in members:
                relative = member.name.removeprefix(root_name).lstrip("/")
                if not relative:
                    continue
                target = (destination / relative).resolve()
                if destination.resolve() not in target.parents:
                    raise ValueError("Archive contains an unsafe path")
                if member.issym() or member.islnk():
                    raise ValueError("Archive links are not supported")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError("Could not read archive member")
                    with source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
    finally:
        archive_path.unlink(missing_ok=True)


@router.post("/install-github")
async def install_from_github(body: GitHubInstall):
    parsed = urlparse(body.url.strip())
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https" or parsed.hostname != "github.com" or len(parts) < 2:
        raise HTTPException(400, "Expected a GitHub repository URL")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    tarball_url = f"https://api.github.com/repos/{owner}/{repo}/tarball/HEAD"
    async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
        response = await client.get(tarball_url, headers={"User-Agent": "Modly-Server"})
        response.raise_for_status()

    root = _extension_root()
    staging = Path(tempfile.mkdtemp(prefix=".modly-install-", dir=root))
    backup = None
    destination = None
    try:
        _extract_github_tarball(response.content, staging)
        manifest_path = staging / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("manifest.json must contain an object")
        extension_id = manifest.get("id")
        if not isinstance(extension_id, str) or not _SAFE_EXTENSION_ID.fullmatch(extension_id):
            raise ValueError("manifest.json contains an invalid extension id")
        if not isinstance(manifest.get("nodes"), list) or not manifest["nodes"]:
            raise ValueError("manifest.json must declare at least one node")
        manifest["source"] = f"https://github.com/{owner}/{repo}"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        destination = _extension_path(extension_id)
        backup = root / f".modly-backup-{extension_id}-{uuid.uuid4().hex}"
        if destination.exists():
            destination.rename(backup)
        staging.rename(destination)
        try:
            if (destination / "setup.py").exists():
                await setup_extension(extension_id)
            elif manifest.get("type") == "process" and (destination / "package.json").exists():
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["npm", "install", "--omit=dev", "--no-audit", "--no-fund"],
                    cwd=destination, capture_output=True, text=True,
                )
                if result.returncode != 0:
                    raise RuntimeError(result.stderr or "npm install failed")
            from services.generator_registry import generator_registry
            await asyncio.to_thread(generator_registry.reload)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            if backup and backup.exists():
                backup.rename(destination)
            raise
        if backup and backup.exists():
            shutil.rmtree(backup)
        return {"success": True, "extensionId": extension_id, "extension": _manifest_for_client(manifest)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


@router.delete("/{extension_id}")
async def uninstall_extension(extension_id: str):
    path = _extension_path(extension_id)
    if path.exists():
        shutil.rmtree(path)
    from services.generator_registry import generator_registry
    await asyncio.to_thread(generator_registry.reload)
    return {"success": True}


@router.post("/reload")
async def reload_extensions(payload: dict | None = Body(default=None)):
    """
    Re-scans the extensions/ folder and reloads the registry without restarting FastAPI.
    Unloads all currently loaded generators before reloading.
    """
    from services.generator_registry import generator_registry
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
    """
    Creates the isolated venv for an extension by running its setup.py.
    Called automatically after installing an extension from GitHub.
    Runs setup.py with Modly's embedded Python and the detected GPU SM.
    """
    from services.generator_registry import EXTENSIONS_DIR

    if EXTENSIONS_DIR is None or not EXTENSIONS_DIR.exists():
        raise HTTPException(400, "EXTENSIONS_DIR not configured")

    ext_dir  = EXTENSIONS_DIR / ext_id
    setup_py = ext_dir / "setup.py"

    if not ext_dir.exists():
        raise HTTPException(404, f"Extension '{ext_id}' not found in {EXTENSIONS_DIR}")
    if not setup_py.exists():
        # No setup.py → legacy extension, nothing to do
        return {"status": "skipped", "reason": "no setup.py"}

    # Detect GPU compute capability. NVIDIA keeps detection priority, exactly
    # like electron/main/gpu-detect.ts: a Ryzen APU beside an NVIDIA dGPU must
    # resolve to CUDA on both code paths.
    gpu_sm, cuda_version = _detect_nvidia_gpu()
    gfx_target           = "" if gpu_sm else _detect_gfx_target()
    flavor               = "cuda" if gpu_sm else ("rocm" if gfx_target else "cpu")

    # Pass arguments as JSON so setup.py sees torch_flavor. The keys mirror
    # runExtensionSetup in electron/main/ipc-handlers.ts exactly — setup.py
    # scripts read the same contract whichever side launched them. Note this
    # endpoint is a fallback: Electron normally runs setup.py itself, and only
    # that path gets the ROCm index rewriting for extensions that ignore
    # torch_flavor.
    args = json.dumps({
        "python_exe":      sys.executable,
        "ext_dir":         str(ext_dir),
        "gpu_sm":          gpu_sm,
        "cuda_version":    cuda_version,
        "accelerator":     flavor,
        "torch_flavor":    flavor,
        "gfx_target":      gfx_target,
        "torch_index_url": _rocm_index_url() if flavor == "rocm" else "",
        "platform":        sys.platform,
        "arch":            _node_arch(),
    })

    # Run setup.py using Modly's embedded Python (sys.executable)
    loop   = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: subprocess.run(
            [sys.executable, str(setup_py), args],
            capture_output=True,
            text=True,
            # pip's own output carries box-drawing characters; the locale codec
            # (cp1252 on Windows) raises on some of them and would turn a
            # successful setup into a 500 with no usable message.
            encoding="utf-8",
            errors="replace",
        )
    )

    if result.returncode != 0:
        raise HTTPException(500, f"setup.py failed:\n{result.stderr}")

    return {
        "status":     "ok",
        "gpu_sm":     gpu_sm,
        "gfx_target": gfx_target,
        "output":     result.stdout,
    }


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
