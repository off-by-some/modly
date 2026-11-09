"""Bounded, transactional installation of extensions fetched by the server."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx

from services.builtin_extensions import BUILTIN_MARKER


MAX_ARCHIVE_BYTES = int(os.environ.get("MODLY_MAX_EXTENSION_ARCHIVE_BYTES", 100 * 1024 * 1024))
MAX_EXTRACTED_BYTES = int(os.environ.get("MODLY_MAX_EXTENSION_EXTRACTED_BYTES", 1024 * 1024 * 1024))
MAX_ARCHIVE_MEMBERS = int(os.environ.get("MODLY_MAX_EXTENSION_ARCHIVE_MEMBERS", 20_000))
SETUP_TIMEOUT_SECONDS = int(os.environ.get("MODLY_EXTENSION_SETUP_TIMEOUT", 30 * 60))
INCOMPLETE_MARKER = ".modly-incomplete"
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_install_lock = asyncio.Lock()

Progress = Callable[[dict], Awaitable[None]]


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
            if declared > MAX_ARCHIVE_BYTES:
                raise ValueError("Extension archive is larger than the server limit")
            with archive_path.open("wb") as output:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    received += len(chunk)
                    if received > MAX_ARCHIVE_BYTES:
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
        if len(members) > MAX_ARCHIVE_MEMBERS:
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
            if total_size > MAX_EXTRACTED_BYTES:
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


async def _run_command(command: list[str], cwd: Path, progress: Progress, message: str) -> None:
    await progress({"step": "setting_up", "message": message})
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert process.stdout is not None

    async def consume() -> None:
        while line := await process.stdout.readline():
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                await progress({"step": "setting_up", "message": text[-1000:]})

    try:
        await asyncio.wait_for(consume(), timeout=SETUP_TIMEOUT_SECONDS)
        code = await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"{message} timed out")
    if code != 0:
        raise RuntimeError(f"{message} failed (exit {code})")


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
        args = json.dumps({
            "python_exe": sys.executable,
            "ext_dir": str(extension_dir),
            "gpu_sm": gpu_sm,
            "cuda_version": cuda_version,
            "accelerator": flavor,
            "torch_flavor": flavor,
            "gfx_target": gfx_target,
            "torch_index_url": _rocm_index_url() if flavor == "rocm" else "",
            "platform": sys.platform,
            "arch": _node_arch(),
        })
        await _run_command([sys.executable, str(setup_py), args], extension_dir, progress, "Setting up Python environment")
    elif is_process and (extension_dir / "package.json").is_file():
        npm = "npm.cmd" if sys.platform == "win32" else "npm"
        await _run_command(
            [npm, "install", "--omit=dev", "--no-audit", "--no-fund", "--ignore-scripts=false"],
            extension_dir,
            progress,
            "Installing extension dependencies",
        )


def recover_interrupted_installs(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for staging in root.glob(".modly-install-*"):
        shutil.rmtree(staging, ignore_errors=True)
    for backup in root.glob(".modly-backup-*"):
        remainder = backup.name.removeprefix(".modly-backup-")
        if "-" not in remainder:
            continue
        extension_id, _transaction_id = remainder.rsplit("-", 1)
        if not _SAFE_ID.fullmatch(extension_id):
            continue
        destination = root / extension_id
        if destination.exists() and not (destination / INCOMPLETE_MARKER).exists():
            shutil.rmtree(backup, ignore_errors=True)
        elif not destination.exists() or (destination / INCOMPLETE_MARKER).exists():
            shutil.rmtree(destination, ignore_errors=True)
            backup.rename(destination)


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
            if destination.exists():
                current, _, _, _ = validate_manifest(destination)
                if current["id"] != extension_id or (current.get("type") == "process") != (manifest.get("type") == "process"):
                    raise ValueError("The existing extension cannot be safely replaced; uninstall it first")

            manifest["source"] = canonical_url
            (extracted / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            (extracted / INCOMPLETE_MARKER).write_text("installing\n", encoding="utf-8")
            backup = root / f".modly-backup-{extension_id}-{uuid.uuid4().hex}"
            if destination.exists():
                destination.rename(backup)
            extracted.rename(destination)
            await _setup_extension(destination, manifest, entry, progress)

            from services.generator_registry import generator_registry
            await asyncio.to_thread(generator_registry.reload)
            errors = generator_registry.load_errors()
            relevant = {key: value for key, value in errors.items() if key == extension_id or key.startswith(f"{extension_id}/")}
            if relevant:
                raise RuntimeError("Extension runtime validation failed: " + "; ".join(relevant.values()))
            (destination / INCOMPLETE_MARKER).unlink(missing_ok=True)
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            await progress({"step": "done", "extensionId": extension_id})
            return {"success": True, "extensionId": extension_id, "manifest": manifest}
        except Exception:
            if destination and destination.exists() and (destination / INCOMPLETE_MARKER).exists():
                shutil.rmtree(destination, ignore_errors=True)
            if destination and backup and backup.exists() and not destination.exists():
                backup.rename(destination)
                try:
                    from services.generator_registry import generator_registry
                    await asyncio.to_thread(generator_registry.reload)
                except Exception:
                    pass
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
