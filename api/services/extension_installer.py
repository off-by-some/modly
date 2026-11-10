"""Bounded, transactional installation of extensions fetched by the server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx

from services.builtin_extensions import BUILTIN_MARKER
from services.extension_runtime import (
    RUNTIME_MARKER,
    docker_capabilities,
    docker_gpu_arguments,
    docker_image_name,
    docker_setup_script,
    dockerfile_for_extension,
    extension_runtime,
    infer_python_version,
    recommended_cuda_lane,
    remove_extension_image,
    runtime_settings,
    write_extension_runtime,
    setup_gpu_evidence,
)
from services.upload_limits import optional_env_limit


MAX_ARCHIVE_BYTES = optional_env_limit("MODLY_MAX_EXTENSION_ARCHIVE_BYTES")
MAX_EXTRACTED_BYTES = optional_env_limit("MODLY_MAX_EXTENSION_EXTRACTED_BYTES")
MAX_ARCHIVE_MEMBERS = optional_env_limit("MODLY_MAX_EXTENSION_ARCHIVE_MEMBERS")
SETUP_TIMEOUT_SECONDS = int(os.environ.get("MODLY_EXTENSION_SETUP_TIMEOUT", 30 * 60))
INCOMPLETE_MARKER = ".modly-incomplete"
REGISTRATION_PENDING_PREFIX = ".modly-registration-pending-"
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_install_lock = asyncio.Lock()

Progress = Callable[[dict], Awaitable[None]]
_logger = logging.getLogger("modly.extensions.install")
_logger.setLevel(logging.INFO)


class InstallCommandError(RuntimeError):
    def __init__(self, summary: str, details: str):
        super().__init__(summary)
        self.details = details


def _remove_path(path: Path) -> None:
    """Remove an extension transaction path without following symlinks."""
    if path.is_symlink():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path, ignore_errors=True)


def extension_setup_environment(
    flavor: str,
    torch_index_url: str = "",
    torch_specs: list[str] | None = None,
) -> dict[str, str]:
    """Build the environment inherited by third-party setup.py processes.

    CUDA extensions commonly make the PyTorch wheel repository pip's primary
    index. Older ensurepip versions can reject a dependency wheel there and
    fall back to an sdist whose build requirements only exist on PyPI. Keep
    PyPI reachable without changing the CUDA index selected by the extension.
    Operators can override or disable the fallback with
    MODLY_EXTENSION_PYPI_INDEX_URL.
    """
    env = dict(os.environ)
    env["MODLY_TORCH_FLAVOR"] = flavor
    env["PYTHONUNBUFFERED"] = "1"
    env["MODLY_TORCH_INDEX_URL"] = torch_index_url
    env["MODLY_TORCH_SPECS"] = json.dumps(torch_specs or [])
    if flavor == "cuda" and "PIP_EXTRA_INDEX_URL" not in env:
        fallback = env.get("MODLY_EXTENSION_PYPI_INDEX_URL", "https://pypi.org/simple").strip()
        if fallback:
            env["PIP_EXTRA_INDEX_URL"] = fallback
    return env


def _registration_state_paths(root: Path, extension_id: str) -> list[Path]:
    prefix = f"{REGISTRATION_PENDING_PREFIX}{extension_id}-"
    return [
        path for path in root.glob(f"{prefix}*")
        if path.name.removeprefix(prefix).isdigit()
    ]


def _begin_registration(root: Path, extension_id: str) -> tuple[dict[str, str], Path]:
    """Create the one-use capability consumed by GeneratorRegistry.reload."""
    token = secrets.token_urlsafe(32)
    for _ in range(10):
        state_name = f"{REGISTRATION_PENDING_PREFIX}{extension_id}-{uuid.uuid4().int}"
        state_path = root / state_name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(state_path, flags, 0o600)
        except FileExistsError:
            continue
        try:
            try:
                if os.name != "nt":
                    os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    descriptor = -1
                    json.dump({
                        "version": 1,
                        "extensionId": extension_id,
                        "destinationName": extension_id,
                        "token": token,
                        "consumed": False,
                    }, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        except BaseException:
            state_path.unlink(missing_ok=True)
            raise
        return ({
            "extensionId": extension_id,
            "destinationName": extension_id,
            "stateName": state_name,
            "token": token,
        }, state_path)
    raise RuntimeError("Could not create extension runtime registration state")


def parse_github_repository(value: str) -> tuple[str, str, str]:
    parsed = urlparse(value.strip())
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(parts) != 2
    ):
        raise ValueError("Expected a GitHub repository URL such as https://github.com/owner/repo")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if not owner or not repo:
        raise ValueError("Expected a GitHub repository URL")
    return owner, repo, f"https://github.com/{owner}/{repo}"


async def _download_archive(url: str, archive_path: Path, progress: Progress) -> None:
    await progress({"step": "downloading", "percent": 0})
    received = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30, read=120)) as client:
        async with client.stream("GET", url, headers={"User-Agent": "Modly-Server"}) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length", "0") or 0)
            if MAX_ARCHIVE_BYTES is not None and declared > MAX_ARCHIVE_BYTES:
                raise ValueError("Extension archive is larger than the server limit")
            with archive_path.open("wb") as output:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    received += len(chunk)
                    if MAX_ARCHIVE_BYTES is not None and received > MAX_ARCHIVE_BYTES:
                        raise ValueError("Extension archive is larger than the server limit")
                    output.write(chunk)
                    percent = min(80, round(received / declared * 80)) if declared else 20
                    await progress({"step": "downloading", "percent": percent})


def _safe_archive_relative(name: str, root_name: str) -> Path | None:
    posix = PurePosixPath(name)
    if posix.is_absolute() or ".." in posix.parts or not posix.parts or posix.parts[0] != root_name:
        raise ValueError("Archive contains an unsafe path")
    remaining = posix.parts[1:]
    return Path(*remaining) if remaining else None


def extract_archive(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if MAX_ARCHIVE_MEMBERS is not None and len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("Extension archive contains too many files")
        roots = {PurePosixPath(member.name).parts[0] for member in members if member.name}
        if len(roots) != 1:
            raise ValueError("GitHub archive has an unexpected layout")
        root_name = next(iter(roots))
        total_size = 0
        destination_resolved = destination.resolve()
        for member in members:
            relative = _safe_archive_relative(member.name, root_name)
            if relative is None:
                continue
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise ValueError("Archive links and special files are not supported")
            total_size += max(0, member.size)
            if MAX_EXTRACTED_BYTES is not None and total_size > MAX_EXTRACTED_BYTES:
                raise ValueError("Extension expands beyond the server limit")
            target = (destination / relative).resolve()
            if destination_resolved not in target.parents:
                raise ValueError("Archive contains an unsafe path")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("Could not read an archive member")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)


def validate_manifest(extension_dir: Path) -> tuple[dict, str, bool, bool]:
    manifest_path = extension_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("manifest.json is missing from the repository")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("manifest.json must contain an object")
    extension_id = manifest.get("id")
    if not isinstance(extension_id, str) or not _SAFE_ID.fullmatch(extension_id):
        raise ValueError("manifest.json contains an invalid extension id")
    nodes = manifest.get("nodes")
    if not isinstance(nodes, list) or not nodes or not all(isinstance(node, dict) and node.get("id") for node in nodes):
        raise ValueError('manifest.json: required field "nodes" missing or empty')
    if manifest.get("model_sources") is not None:
        raise ValueError("manifest.json: model_sources must be declared on a model node")

    is_process = manifest.get("type") == "process"
    entry = manifest.get("entry") or "processor.js"
    if not isinstance(entry, str) or Path(entry).is_absolute() or ".." in Path(entry).parts:
        raise ValueError("manifest.json contains an unsafe entry path")
    if is_process:
        if manifest.get("weight_groups") is not None:
            raise ValueError("manifest.json: weight_groups is supported only for model extensions")
        if not (extension_dir / entry).is_file():
            raise ValueError(f'manifest.json: entry file "{entry}" is missing')
        for node in nodes:
            if node.get("model_sources") is not None or node.get("weight_groups") is not None or node.get("weight_variants") is not None:
                raise ValueError("manifest.json: model weights are supported only for model extensions")
            inputs = node.get("inputs") if isinstance(node.get("inputs"), list) else [node.get("input", "image")]
            if "scene" in inputs or node.get("output", "mesh") == "scene":
                raise ValueError("manifest.json: scene input and output are supported only for model nodes")
    else:
        if not (extension_dir / "generator.py").is_file():
            raise ValueError("generator.py is missing from the repository")
        if not isinstance(manifest.get("generator_class"), str) or not manifest["generator_class"]:
            raise ValueError('manifest.json: required field "generator_class" missing')
    return manifest, entry, is_process, is_process and entry.endswith(".py")


async def _run_command(
    command: list[str],
    cwd: Path,
    progress: Progress,
    message: str,
    *,
    env: dict[str, str] | None = None,
) -> None:
    await progress({"step": "setting_up", "message": message})
    _logger.info("%s: %s", message, " ".join(command))
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**(env or os.environ), "PYTHONUTF8": "1"},
    )
    assert process.stdout is not None
    # Docker setup can emit several hundred lines after the actual nested pip
    # failure (for example, a structured setup report followed by BuildKit's
    # summary). Retain enough context for the terminal event to include the
    # original cause instead of only the outer `docker build` error.
    output_lines: deque[str] = deque(maxlen=2000)

    async def consume() -> None:
        while line := await process.stdout.readline():
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if text.strip():
                output_lines.append(text[:4000])
                _logger.info("%s", text)
                await progress({"step": "setting_up", "message": text[:1000]})

    try:
        await asyncio.wait_for(consume(), timeout=SETUP_TIMEOUT_SECONDS)
        code = await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise InstallCommandError(
            f"{message} timed out",
            "\n".join(output_lines) or "The command timed out without producing output.",
        )
    if code != 0:
        raise InstallCommandError(
            f"{message} failed (exit {code})",
            "\n".join(output_lines) or "The command exited without producing output.",
        )


async def _compile_typescript(extension_dir: Path, entry: str, progress: Progress) -> str:
    output = str(Path(entry).with_suffix(".js"))
    project_root = Path(__file__).resolve().parents[2]
    script = (
        "require('esbuild').buildSync({entryPoints:[process.argv[1]],outfile:process.argv[2],"
        "bundle:true,platform:'node',format:'cjs',external:['electron']})"
    )
    await _run_command(
        [os.environ.get("MODLY_NODE_EXECUTABLE", "node"), "-e", script, str(extension_dir / entry), str(extension_dir / output)],
        project_root,
        progress,
        "Compiling TypeScript entry",
    )
    return output


async def _setup_extension(extension_dir: Path, manifest: dict, entry: str, progress: Progress) -> None:
    is_process = manifest.get("type") == "process"
    if entry.endswith(".ts"):
        entry = await _compile_typescript(extension_dir, entry, progress)
        manifest["entry"] = entry
        (extension_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    setup_py = extension_dir / "setup.py"
    if setup_py.is_file():
        # Reuse the same hardware contract as the desktop-side installer.
        from routers.extensions import _detect_gfx_target, _detect_nvidia_gpu, _node_arch, _rocm_index_url
        gpu_sm, cuda_version = _detect_nvidia_gpu()
        gfx_target = "" if gpu_sm else _detect_gfx_target()
        flavor = "cuda" if gpu_sm else ("rocm" if gfx_target else "cpu")
        torch_index_url = _rocm_index_url() if flavor == "rocm" else ""
        torch_specs = ["torch", "torchvision"] if flavor == "rocm" else []
        args = json.dumps({
            "python_exe": sys.executable,
            "ext_dir": str(extension_dir),
            "accelerator": flavor,
            "torch_flavor": flavor,
            "gfx_target": gfx_target,
            "torch_index_url": torch_index_url,
            "platform": sys.platform,
            "arch": _node_arch(),
            **setup_gpu_evidence(gpu_sm, cuda_version),
        })
        await _run_command(
            [sys.executable, "-u", str(setup_py), args],
            extension_dir,
            progress,
            "Setting up Python environment",
            env=extension_setup_environment(flavor, torch_index_url, torch_specs),
        )
    elif is_process and (extension_dir / "package.json").is_file():
        npm = "npm.cmd" if sys.platform == "win32" else "npm"
        await _run_command(
            [npm, "install", "--omit=dev", "--no-audit", "--no-fund", "--ignore-scripts=false"],
            extension_dir,
            progress,
            "Installing extension dependencies",
        )


async def _setup_docker_extension(
    root: Path,
    extension_dir: Path,
    manifest: dict,
    entry: str,
    progress: Progress,
) -> dict:
    capabilities = await asyncio.to_thread(docker_capabilities, root)
    if not capabilities["dockerAvailable"]:
        raise RuntimeError(capabilities["reason"] or "Docker is not available on this server")

    typescript_entry = entry if entry.endswith(".ts") else ""

    from routers.extensions import _detect_nvidia_gpu
    gpu_sm, detected_cuda = _detect_nvidia_gpu()
    cuda_version = recommended_cuda_lane(gpu_sm, detected_cuda)
    python_version = infer_python_version(extension_dir, manifest)
    image = docker_image_name(extension_dir, manifest)
    has_python_setup = (extension_dir / "setup.py").is_file()
    bootstrap_image = f"{image}-bootstrap" if has_python_setup else image
    docker = shutil.which("docker") or "docker"
    dockerfile = dockerfile_for_extension(
        extension_dir,
        manifest,
        python_version,
        gpu_sm,
        cuda_version,
        typescript_entry,
    )
    # Build from a server-created context. A repository-controlled
    # .dockerignore must not be able to silently omit its manifest, entrypoint,
    # or setup files, and old host venvs must never be copied into an image.
    with tempfile.TemporaryDirectory(prefix="modly-extension-build-") as temporary:
        build_context = Path(temporary) / "context"
        await asyncio.to_thread(
            shutil.copytree,
            extension_dir,
            build_context,
            symlinks=False,
            ignore=shutil.ignore_patterns(
                ".git", ".dockerignore", "venv", ".venv", "node_modules",
                "__pycache__", "*.pyc", INCOMPLETE_MARKER, RUNTIME_MARKER,
            ),
        )
        dockerfile_path = build_context / ".modly.Dockerfile"
        dockerfile_path.write_text(dockerfile, encoding="utf-8")
        await _run_command(
            [
                shutil.which("docker") or "docker",
                "build",
                "--progress",
                "plain",
                "--tag",
                bootstrap_image,
                "--file",
                str(dockerfile_path),
                ".",
            ],
            build_context,
            progress,
            f"Building Docker runtime (Python {python_version})",
        )

    if has_python_setup:
        safe_id = re.sub(
            r"[^a-z0-9_.-]+", "-", str(manifest.get("id", extension_dir.name)).lower()
        )[:32]
        container_name = f"modly-extension-setup-{safe_id}-{uuid.uuid4().hex[:12]}"
        setup_script = docker_setup_script(extension_dir, gpu_sm, cuda_version)

        def cleanup(command: list[str]) -> None:
            try:
                subprocess.run(
                    command,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass

        try:
            await _run_command(
                [
                    docker,
                    "run",
                    "--name",
                    container_name,
                    *docker_gpu_arguments(str(capabilities.get("nvidiaContainerMode", ""))),
                    bootstrap_image,
                    "/bin/sh",
                    "-c",
                    setup_script,
                ],
                extension_dir,
                progress,
                "Preparing Docker runtime with extension setup",
            )
            await _run_command(
                [
                    docker,
                    "commit",
                    "--change",
                    'CMD ["/opt/modly/runtime-python", "/modly/api/runner.py"]',
                    container_name,
                    image,
                ],
                extension_dir,
                progress,
                "Finalizing Docker runtime",
            )
        except BaseException:
            await asyncio.to_thread(remove_extension_image, {"image": image})
            raise
        finally:
            await asyncio.to_thread(cleanup, [docker, "container", "rm", "--force", container_name])
            await asyncio.to_thread(cleanup, [docker, "image", "rm", bootstrap_image])

    metadata = {
        "kind": "docker",
        "image": image,
        "python": python_version,
        "cudaVersion": cuda_version,
        "gpuSm": gpu_sm,
        # The image is CUDA-capable based on the server GPU lane. Toolkit
        # availability is evaluated at invocation time so installing or fixing
        # the toolkit later immediately enables GPU passthrough.
        "nvidia": bool(gpu_sm),
        **({"entry": str(Path(typescript_entry).with_suffix(".js"))}
           if typescript_entry else {}),
    }
    write_extension_runtime(extension_dir, metadata)
    return metadata


def recover_interrupted_installs(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for staging in root.glob(".modly-install-*"):
        _remove_path(staging)
    pending_ids = {
        remainder.rsplit("-", 1)[0]
        for state in root.glob(f"{REGISTRATION_PENDING_PREFIX}*")
        if (
            (remainder := state.name.removeprefix(REGISTRATION_PENDING_PREFIX))
            and "-" in remainder
            and remainder.rsplit("-", 1)[1].isdigit()
            and _SAFE_ID.fullmatch(remainder.rsplit("-", 1)[0])
        )
    }
    for backup in root.glob(".modly-backup-*"):
        remainder = backup.name.removeprefix(".modly-backup-")
        if "-" not in remainder:
            continue
        extension_id, _transaction_id = remainder.rsplit("-", 1)
        if not _SAFE_ID.fullmatch(extension_id):
            continue
        destination = root / extension_id
        if extension_id in pending_ids or not destination.exists() or (destination / INCOMPLETE_MARKER).exists():
            if destination.exists() or destination.is_symlink():
                remove_extension_image(extension_runtime(destination))
            _remove_path(destination)
            backup.rename(destination)
            for state in _registration_state_paths(root, extension_id):
                state.unlink(missing_ok=True)
            pending_ids.discard(extension_id)
        else:
            _remove_path(backup)
    # A fresh install has no backup. Pending registration still means its
    # runtime was never committed, so discard it instead of exposing an
    # unvalidated extension after restart.
    for extension_id in pending_ids:
        destination = root / extension_id
        if destination.exists() or destination.is_symlink():
            remove_extension_image(extension_runtime(destination))
        _remove_path(root / extension_id)
        for state in _registration_state_paths(root, extension_id):
            state.unlink(missing_ok=True)


async def install_github(url: str, root: Path, progress: Progress) -> dict:
    owner, repo, canonical_url = parse_github_repository(url)
    root.mkdir(parents=True, exist_ok=True)
    async with _install_lock:
        work = Path(tempfile.mkdtemp(prefix=".modly-install-", dir=root))
        archive_path = work / "repository.tar.gz"
        extracted = work / "extension"
        extracted.mkdir()
        destination: Path | None = None
        backup: Path | None = None
        registration_state: Path | None = None
        activated = False
        previous_runtime: dict = {}
        installed_runtime: dict = {}
        try:
            await _download_archive(f"https://api.github.com/repos/{owner}/{repo}/tarball/HEAD", archive_path, progress)
            await progress({"step": "extracting"})
            await asyncio.to_thread(extract_archive, archive_path, extracted)
            await progress({"step": "validating"})
            manifest, entry, _, _ = validate_manifest(extracted)
            extension_id = manifest["id"]
            destination = root / extension_id
            if (destination / BUILTIN_MARKER).is_file():
                raise ValueError("Built-in extensions are managed by the server and cannot be replaced")
            if destination.exists() or destination.is_symlink():
                previous_runtime = extension_runtime(destination)
                current, _, _, _ = validate_manifest(destination)
                if current["id"] != extension_id or (current.get("type") == "process") != (manifest.get("type") == "process"):
                    raise ValueError("The existing extension cannot be safely replaced; uninstall it first")

            manifest["source"] = canonical_url
            (extracted / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            (extracted / INCOMPLETE_MARKER).write_text("installing\n", encoding="utf-8")
            backup = root / f".modly-backup-{extension_id}-{uuid.uuid4().hex}"
            if destination.exists() or destination.is_symlink():
                destination.rename(backup)
            extracted.rename(destination)
            activated = True
            if runtime_settings(root)["useDocker"]:
                installed_runtime = await _setup_docker_extension(
                    root, destination, manifest, entry, progress
                )
            else:
                installed_runtime = {"kind": "system"}
                write_extension_runtime(destination, installed_runtime)
                await _setup_extension(destination, manifest, entry, progress)

            from services.generator_registry import generator_registry
            validation_capability, registration_state = _begin_registration(root, extension_id)
            (destination / INCOMPLETE_MARKER).unlink()
            await asyncio.to_thread(generator_registry.reload, validation_capability)
            errors = generator_registry.load_errors()
            relevant = {key: value for key, value in errors.items() if key == extension_id or key.startswith(f"{extension_id}/")}
            if relevant:
                raise RuntimeError("Extension runtime validation failed: " + "; ".join(relevant.values()))
            registration_state.unlink(missing_ok=True)
            registration_state = None
            if backup.exists() or backup.is_symlink():
                _remove_path(backup)
            if previous_runtime.get("image") != installed_runtime.get("image"):
                await asyncio.to_thread(remove_extension_image, previous_runtime)
            await progress({"step": "done", "extensionId": extension_id})
            return {"success": True, "extensionId": extension_id, "manifest": manifest}
        except Exception:
            await asyncio.to_thread(remove_extension_image, installed_runtime)
            if activated and destination and (destination.exists() or destination.is_symlink()):
                _remove_path(destination)
            if destination and backup and (backup.exists() or backup.is_symlink()) and not destination.exists() and not destination.is_symlink():
                backup.rename(destination)
            if registration_state is not None:
                registration_state.unlink(missing_ok=True)
            if activated:
                try:
                    from services.generator_registry import generator_registry
                    await asyncio.to_thread(generator_registry.reload)
                except Exception:
                    pass
            raise
        finally:
            _remove_path(work)
