"""Install the server-owned workflow extensions into its extension directory."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import hashlib
from pathlib import Path


BUILTIN_MARKER = ".modly-builtin"


def _source_digest(source_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(source_root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(source_root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _build_repository_builtins(destination_root: Path) -> Path | None:
    project_root = Path(__file__).resolve().parents[2]
    source_root = project_root / "src" / "areas" / "workflows" / "nodes"
    if not source_root.is_dir():
        return None
    cache = destination_root.parent / ".builtin-extensions-cache"
    digest = _source_digest(source_root)
    digest_file = cache / ".source-digest"
    if cache.is_dir() and digest_file.is_file() and digest_file.read_text(encoding="utf-8", errors="ignore").strip() == digest:
        return cache

    staging = cache.with_name(f"{cache.name}.staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for source in sorted(source_root.iterdir()):
        if not source.is_dir() or not (source / "manifest.json").is_file():
            continue
        target = staging / source.name
        target.mkdir()
        shutil.copy2(source / "manifest.json", target / "manifest.json")
        if (source / "package.json").is_file():
            shutil.copy2(source / "package.json", target / "package.json")
        for processor in source.glob("processor.py"):
            shutil.copy2(processor, target / processor.name)
        typescript = source / "processor.ts"
        if typescript.is_file():
            subprocess.run(
                [
                    "npx", "--yes", "esbuild", str(typescript), "--bundle",
                    "--platform=node", "--format=cjs", f"--outfile={target / 'processor.js'}",
                    "--external:electron",
                ],
                cwd=project_root,
                check=True,
                timeout=10 * 60,
            )
        if (target / "package.json").is_file():
            npm = "npm.cmd" if os.name == "nt" else "npm"
            subprocess.run(
                [npm, "install", "--omit=dev", "--no-audit", "--no-fund", "--ignore-scripts=false"],
                cwd=target,
                check=True,
                timeout=30 * 60,
            )
    (staging / ".source-digest").write_text(digest + "\n", encoding="utf-8")
    shutil.rmtree(cache, ignore_errors=True)
    staging.rename(cache)
    return cache


def _source_directory(destination_root: Path) -> Path | None:
    configured = os.environ.get("MODLY_BUILTIN_EXTENSIONS_DIR", "").strip()
    api_dir = Path(__file__).resolve().parents[1]
    candidates = [
        Path(configured).expanduser() if configured else None,
        api_dir.parent / "builtin-extensions",       # packaged app resources
        api_dir.parent / "out" / "builtin-extensions",  # repository checkout
        api_dir / "resources" / "builtin-extensions",
    ]
    found = next((path.resolve() for path in candidates if path and path.is_dir()), None)
    return found or _build_repository_builtins(destination_root)


def sync_builtin_extensions(destination_root: Path) -> list[str]:
    """Refresh built-ins without touching user-installed extensions."""
    source_root = _source_directory(destination_root)
    if source_root is None:
        return []

    destination_root.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for source in sorted(source_root.iterdir()):
        manifest_path = source / "manifest.json"
        if not source.is_dir() or not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        extension_id = manifest.get("id") if isinstance(manifest, dict) else None
        if not isinstance(extension_id, str) or extension_id != source.name:
            continue

        destination = destination_root / extension_id
        staging = destination_root / f".modly-builtin-{extension_id}.staging"
        backup = destination_root / f".modly-builtin-{extension_id}.backup"
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
        shutil.copytree(source, staging, symlinks=False)
        manifest["builtin"] = True
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        (staging / BUILTIN_MARKER).write_text("server-owned\n", encoding="utf-8")

        if destination.exists():
            # Never overwrite a user extension merely because it happens to use a
            # built-in id. Only destinations previously created by this sync are
            # server-owned and safe to refresh.
            if not (destination / BUILTIN_MARKER).is_file():
                shutil.rmtree(staging, ignore_errors=True)
                continue
            destination.rename(backup)
        try:
            staging.rename(destination)
        except Exception:
            if backup.exists() and not destination.exists():
                backup.rename(destination)
            raise
        shutil.rmtree(backup, ignore_errors=True)
        installed.append(extension_id)
    return installed

