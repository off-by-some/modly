"""Server-owned runtime selection and Docker support for URL extensions."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from services.builtin_extensions import BUILTIN_MARKER


RUNTIME_MARKER = ".modly-runtime.json"
RUNTIME_SETTINGS = ".modly-runtime-settings.json"
_PYTHON_VERSION = re.compile(r"^(?:cp3(?P<cp_minor>\d{1,2})|3\.(?P<minor>\d{1,2}))(?:\..*)?$")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def runtime_settings(root: Path) -> dict:
    try:
        value = json.loads((root / RUNTIME_SETTINGS).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        value = {}
    return {"useDocker": value.get("useDocker") is True}


def set_docker_enabled(root: Path, enabled: bool) -> dict:
    _atomic_json(root / RUNTIME_SETTINGS, {"version": 1, "useDocker": bool(enabled)})
    return runtime_settings(root)


def extension_runtime(extension_dir: Path) -> dict:
    if (extension_dir / BUILTIN_MARKER).is_file():
        return {"kind": "builtin"}
    try:
        value = json.loads((extension_dir / RUNTIME_MARKER).read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("kind") in {"system", "docker"}:
            return value
    except (OSError, ValueError, TypeError):
        pass
    # Extensions installed before runtime selection existed are system installs.
    return {"kind": "system"}


def write_extension_runtime(extension_dir: Path, value: dict) -> None:
    _atomic_json(extension_dir / RUNTIME_MARKER, {"version": 1, **value})


def extension_is_visible(root: Path, extension_dir: Path) -> bool:
    kind = extension_runtime(extension_dir).get("kind")
    if kind == "builtin":
        return True
    desired = "docker" if runtime_settings(root)["useDocker"] else "system"
    return kind == desired


def _run_probe(command: list[str], timeout: float = 5) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (result.stdout or result.stderr or "").strip()
    return result.returncode == 0, output


def docker_capabilities(root: Path) -> dict:
    docker = shutil.which("docker")
    available = False
    version = ""
    reason = "Docker is not installed on this server."
    if docker:
        available, version = _run_probe([docker, "version", "--format", "{{.Server.Version}}"])
        if not available:
            reason = "Docker is installed, but its daemon is not available to the Modly server."

    toolkit_installed = False
    container_mode = ""
    if available:
        ok, runtimes = _run_probe([docker, "info", "--format", "{{json .Runtimes}}"])
        # Legacy NVIDIA Container Runtime configuration. This is the path used
        # by `docker run --gpus all` and remains the most widely deployed mode.
        if ok and "nvidia" in runtimes.lower():
            toolkit_installed = True
            container_mode = "runtime"

        nvidia_ctk = shutil.which("nvidia-ctk")
        nvidia_container_cli = shutil.which("nvidia-container-cli")
        for executable in (nvidia_ctk, nvidia_container_cli):
            if executable:
                probe_ok, _ = _run_probe([executable, "--version"])
                toolkit_installed = toolkit_installed or probe_ok

        # Docker Engine can also consume NVIDIA's generated CDI specification
        # without exposing a runtime literally named `nvidia`. Verify both the
        # daemon-side CDI facility and an actual all-GPU NVIDIA CDI device; a
        # binary on PATH alone does not prove Docker can inject the GPU.
        if not container_mode and nvidia_ctk:
            docker_cdi_ok, cdi_directories = _run_probe(
                [docker, "info", "--format", "{{json .CDISpecDirs}}"]
            )
            cdi_ok, cdi_devices = _run_probe([nvidia_ctk, "cdi", "list"])
            if (
                docker_cdi_ok
                and cdi_directories.strip() not in {"", "null", "[]"}
                and cdi_ok
                and "nvidia.com/gpu=all" in cdi_devices.lower()
            ):
                toolkit_installed = True
                container_mode = "cdi"

    selected = runtime_settings(root)["useDocker"]
    return {
        "useDocker": selected and available,
        "configuredUseDocker": selected,
        "dockerAvailable": available,
        "dockerVersion": version if available else "",
        "nvidiaToolkitInstalled": toolkit_installed,
        "nvidiaToolkitAvailable": bool(container_mode),
        "nvidiaContainerMode": container_mode,
        "reason": "" if available else reason,
        "serverPlatform": platform.system().lower(),
    }


def _normalise_python_version(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = _PYTHON_VERSION.fullmatch(value.strip().lower())
    if not match:
        return None
    minor = int(match.group("cp_minor") or match.group("minor"))
    if minor < 8 or minor > 14:
        return None
    return f"3.{minor}"


def infer_python_version(extension_dir: Path, manifest: dict) -> str:
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    for candidate in (
        runtime.get("python"),
        manifest.get("python_version"),
        manifest.get("python"),
    ):
        if version := _normalise_python_version(candidate):
            return version

    # Existing wheelhouse manifests provide the strongest machine-readable
    # evidence. Prefer Linux tags matching this server's architecture.
    candidates: list[str] = []

    def collect_tags(value: object, inherited_os: str = "") -> None:
        if isinstance(value, dict):
            os_name = value.get("os", inherited_os)
            if not isinstance(os_name, str):
                os_name = inherited_os
            if os_name.lower() in {"", "linux"}:
                if version := _normalise_python_version(value.get("python_tag")):
                    candidates.append(version)
            for child in value.values():
                collect_tags(child, os_name)
        elif isinstance(value, list):
            for child in value:
                collect_tags(child, inherited_os)

    for wheelhouse in extension_dir.rglob("wheelhouse.manifest.json"):
        try:
            payload = json.loads(wheelhouse.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        collect_tags(payload)
    if candidates:
        return sorted(candidates, key=lambda item: tuple(map(int, item.split("."))), reverse=True)[0]

    # A standardized requires-python declaration is the next-best signal. We
    # intentionally accept only an unambiguous lower bound; complex specifier
    # solving belongs in a future extension manifest schema.
    for metadata in (extension_dir / "pyproject.toml", extension_dir / "setup.cfg"):
        try:
            text = metadata.read_text(encoding="utf-8")
        except OSError:
            continue
        match = re.search(
            r"(?:requires-python|python_requires)\s*=\s*['\"]?\s*>=\s*(3\.\d{1,2})",
            text,
            re.IGNORECASE,
        )
        if match and (version := _normalise_python_version(match.group(1))):
            return version
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _source_digest(extension_dir: Path) -> str:
    digest = hashlib.sha256()
    ignored = {RUNTIME_MARKER, ".modly-incomplete", "venv", ".venv", "node_modules", "__pycache__"}
    for path in sorted(extension_dir.rglob("*")):
        relative = path.relative_to(extension_dir)
        if any(part in ignored for part in relative.parts) or not path.is_file():
            continue
        digest.update(str(relative).encode())
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        except OSError:
            continue
    return digest.hexdigest()[:16]


def recommended_cuda_lane(gpu_sm: str | int, detected_cuda: str | int) -> int:
    """Return the integer CUDA lane used by extension setup.py contracts.

    Existing extensions expect values such as 124/128, matching both the
    desktop GPU detector and the server's ``_detect_nvidia_gpu`` helper.
    Docker image metadata may display this value however it likes, but setup
    must not receive a dotted, incompatible representation.
    """
    try:
        sm = int(gpu_sm)
    except (TypeError, ValueError):
        sm = 0
    if sm >= 100:
        return 128
    if sm >= 70:
        return 124
    if not detected_cuda:
        return 0
    value = str(detected_cuda).strip()
    if value.isdigit():
        return int(value)
    match = re.fullmatch(r"(\d+)\.(\d+)", value)
    return int(match.group(1)) * 10 + int(match.group(2)) if match else 0


def docker_image_name(
    extension_dir: Path,
    manifest: dict,
    transaction_id: str | None = None,
) -> str:
    safe_id = re.sub(r"[^a-z0-9_.-]+", "-", str(manifest.get("id", extension_dir.name)).lower())
    transaction = re.sub(r"[^a-f0-9]", "", transaction_id or uuid.uuid4().hex)[:12]
    return f"modly-extension-{safe_id}:{_source_digest(extension_dir)}-{transaction}"


def remove_extension_image(runtime: dict) -> None:
    image = runtime.get("image") if isinstance(runtime, dict) else None
    if not isinstance(image, str) or not re.fullmatch(
        r"modly-extension-[a-z0-9_.-]+:[a-f0-9]{16}(?:-[a-f0-9]{12})?", image
    ):
        return
    docker = shutil.which("docker")
    if not docker:
        return
    try:
        subprocess.run(
            [docker, "image", "rm", image],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def dockerfile_for_extension(
    extension_dir: Path,
    manifest: dict,
    python_version: str,
    gpu_sm: str,
    cuda_version: int,
    typescript_entry: str = "",
) -> str:
    setup_steps: list[str] = []
    accelerator_environment = ""
    if manifest.get("type") == "process" and (extension_dir / "package.json").is_file():
        setup_steps.append("RUN npm install --omit=dev --no-audit --no-fund --ignore-scripts=false")
    if typescript_entry:
        output = str(Path(typescript_entry).with_suffix(".js"))
        setup_steps.append(
            "RUN npx --yes esbuild "
            f"{json.dumps(typescript_entry)} --bundle --platform=node --format=cjs "
            f"--external:electron --outfile={json.dumps(output)}"
        )
    if (extension_dir / "setup.py").is_file():
        accelerator = "cuda" if gpu_sm else "cpu"
        if accelerator == "cuda":
            accelerator_environment = "ENV PIP_EXTRA_INDEX_URL=https://pypi.org/simple MODLY_TORCH_FLAVOR=cuda\n"
        payload = json.dumps({
            "python_exe": "/usr/local/bin/python",
            "ext_dir": "/opt/modly/extension",
            "gpu_sm": gpu_sm,
            "cuda_version": cuda_version,
            "accelerator": accelerator,
            "torch_flavor": accelerator,
            "gfx_target": "",
            "torch_index_url": "",
            "platform": "linux",
            "arch": "x64" if platform.machine().lower() in {"x86_64", "amd64"} else "arm64",
        })
        setup_steps.append(f"RUN python setup.py {json.dumps(payload)}")
    setup = "\n".join(setup_steps)
    if setup:
        setup += "\n"
    return f"""FROM python:{python_version}-slim-bookworm
ENV DEBIAN_FRONTEND=noninteractive PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake git libgl1 libglib2.0-0 libgomp1 ninja-build nodejs npm && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/modly/extension
COPY . .
{accelerator_environment}{setup}RUN set -eux; python_path=/usr/local/bin/python; if [ -x venv/bin/python ]; then python_path=/opt/modly/extension/venv/bin/python; elif [ -x .venv/bin/python ]; then python_path=/opt/modly/extension/.venv/bin/python; fi; ln -sf "$python_path" /opt/modly/runtime-python
CMD ["/opt/modly/runtime-python", "/modly/api/runner.py"]
"""


def docker_run_prefix(
    image: str,
    *,
    name: str,
    models_dir: Path,
    workspace_dir: Path,
    api_dir: Path,
    cache_dir: Path,
    nvidia_mode: str = "",
    environment: dict[str, str | None] | None = None,
) -> list[str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    docker = shutil.which("docker") or "docker"
    command = [
        docker, "run", "--rm", "-i", "--name", name,
        "--label", "com.modly.runtime=extension",
        "-v", f"{models_dir.resolve()}:/modly/models",
        "-v", f"{workspace_dir.resolve()}:/modly/workspace",
        "-v", f"{api_dir.resolve()}:/modly/api:ro",
        "-v", f"{cache_dir.resolve()}:/modly/cache",
    ]
    if platform.system() == "Linux":
        # The bundled API intentionally binds loopback. Host networking lets a
        # local Linux container reach it without exposing the API to the LAN.
        command += ["--network", "host"]
        if hasattr(os, "getuid") and hasattr(os, "getgid"):
            # Files written to model/workspace/cache mounts must remain owned
            # and removable by the API server user.
            command += ["--user", f"{os.getuid()}:{os.getgid()}"]
    else:
        command += ["--add-host", "host.docker.internal:host-gateway"]
    if nvidia_mode == "runtime":
        command += ["--gpus", "all"]
    elif nvidia_mode == "cdi":
        command += ["--device", "nvidia.com/gpu=all"]
    runtime_environment: dict[str, str | None] = {
        "HOME": "/tmp",
        "XDG_CACHE_HOME": "/modly/cache",
        "HF_HOME": "/modly/cache/huggingface",
        "TORCH_HOME": "/modly/cache/torch",
        "PIP_CACHE_DIR": "/modly/cache/pip",
        **(environment or {}),
    }
    for key, value in runtime_environment.items():
        command += ["-e", key if value is None else f"{key}={value}"]
    return command + [image]


def container_api_url() -> str:
    override = os.environ.get("MODLY_CONTAINER_API_URL", "").strip()
    if override:
        return override.rstrip("/")
    port = os.environ.get("MODLY_API_PORT", "8765").strip()
    if not port.isdigit():
        port = "8765"
    host = "127.0.0.1" if platform.system() == "Linux" else "host.docker.internal"
    return f"http://{host}:{port}"
