import asyncio
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import services.generator_registry as registry_module
from routers.extensions import (
    GitHubInstall,
    _install_jobs,
    install_from_github,
    install_github_status,
    reload_extensions,
)
from services.builtin_extensions import BUILTIN_MARKER, sync_builtin_extensions
from services.extension_installer import (
    InstallCommandError,
    _run_command,
    extension_setup_environment,
    extract_archive,
    install_github,
    parse_github_repository,
    recover_interrupted_installs,
    validate_manifest,
)
from services.upload_limits import optional_env_limit


class _FakeRegistry:
    def __init__(self) -> None:
        self._generators = {"healthy/generate": object()}
        self.reload_calls: list[object] = []

    def reload(self, validation_capability: object = None) -> None:
        self.reload_calls.append(validation_capability)

    def load_errors(self) -> dict[str, str]:
        return {"pending/generate": "quarantined"}


class ExtensionReloadRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_registry = registry_module.generator_registry
        self.registry = _FakeRegistry()
        registry_module.generator_registry = self.registry

    def tearDown(self) -> None:
        registry_module.generator_registry = self.previous_registry

    def test_predictable_extension_id_is_ignored_and_response_shape_is_stable(self) -> None:
        response = asyncio.run(reload_extensions({
            "validatingExtensionId": "pending",
        }))

        self.assertEqual(self.registry.reload_calls, [None])
        self.assertEqual(
            response,
            {
                "reloaded": True,
                "models": ["healthy/generate"],
                "errors": {"pending/generate": "quarantined"},
            },
        )

    def test_only_nested_validation_capability_is_forwarded(self) -> None:
        capability = {
            "extensionId": "pending",
            "destinationName": "pending",
            "stateName": ".modly-registration-pending-pending-100",
            "token": "t" * 43,
        }

        asyncio.run(reload_extensions({
            "validationCapability": capability,
        }))

        self.assertEqual(self.registry.reload_calls, [capability])


class ExtensionInstallStatusTests(unittest.TestCase):
    def test_install_continues_after_the_progress_stream_disconnects(self) -> None:
        async def fake_install(_url: str, _root: Path, progress) -> dict:
            await progress({"step": "setting_up", "message": "Downloading a large wheel"})
            await asyncio.sleep(0.01)
            return {
                "success": True,
                "extensionId": "example",
                "manifest": {
                    "id": "example",
                    "type": "model",
                    "generator_class": "Example",
                    "nodes": [{"id": "generate"}],
                },
            }

        async def scenario(root: Path) -> dict:
            with patch("services.extension_installer.install_github", new=fake_install):
                response = await install_from_github(GitHubInstall(url="https://github.com/acme/example"))
                install_id = response.headers["x-modly-install-id"]
                stream = response.body_iterator
                await anext(stream)
                await stream.aclose()
                for _ in range(50):
                    status = await install_github_status(install_id)
                    if status["done"]:
                        _install_jobs.pop(install_id, None)
                        return status
                    await asyncio.sleep(0.01)
                self.fail("server-owned install did not finish after stream disconnect")

        with tempfile.TemporaryDirectory() as tmp:
            previous_root = registry_module.EXTENSIONS_DIR
            registry_module.EXTENSIONS_DIR = Path(tmp)
            try:
                status = asyncio.run(scenario(Path(tmp)))
            finally:
                registry_module.EXTENSIONS_DIR = previous_root

        self.assertEqual(status["terminal"]["step"], "result")
        self.assertEqual(status["terminal"]["extensionId"], "example")

    def test_terminal_install_status_survives_a_lost_progress_stream(self) -> None:
        install_id = "a" * 32
        terminal = {"step": "result", "success": True, "extensionId": "example"}
        _install_jobs[install_id] = {
            "latest": terminal,
            "terminal": terminal,
            "updated_at": 1e30,
        }
        try:
            status = asyncio.run(install_github_status(install_id))
        finally:
            _install_jobs.pop(install_id, None)

        self.assertTrue(status["done"])
        self.assertEqual(status["terminal"], terminal)


class ServerExtensionInstallerTests(unittest.TestCase):
    def test_server_install_clears_quarantine_only_for_authorized_validation(self) -> None:
        async def fake_download(_url: str, _archive: Path, progress) -> None:
            await progress({"step": "downloading", "percent": 80})

        def fake_extract(_archive: Path, destination: Path) -> None:
            (destination / "manifest.json").write_text(json.dumps({
                "id": "example",
                "type": "model",
                "generator_class": "TestGenerator",
                "nodes": [{"id": "generate"}],
            }))
            (destination / "generator.py").write_text(
                "from services.generators.base import BaseGenerator\n"
                "class TestGenerator(BaseGenerator):\n"
                "    def load(self): pass\n"
                "    def generate(self, image_bytes, params, progress_cb=None, cancel_event=None):\n"
                "        return self.outputs_dir / 'result.glb'\n"
            )

        async def fake_setup(_destination: Path, _manifest: dict, _entry: str, _progress) -> None:
            return None

        async def scenario(root: Path) -> tuple[dict, list[dict]]:
            events: list[dict] = []

            async def progress(event: dict) -> None:
                events.append(event)

            with (
                patch("services.extension_installer._download_archive", new=fake_download),
                patch("services.extension_installer.extract_archive", new=fake_extract),
                patch("services.extension_installer._setup_extension", new=fake_setup),
            ):
                result = await install_github("https://github.com/acme/example", root, progress)
            return result, events

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous_root = registry_module.EXTENSIONS_DIR
            previous_registry = registry_module.generator_registry
            registry_module.EXTENSIONS_DIR = root
            registry_module.generator_registry = registry_module.GeneratorRegistry()
            try:
                result, events = asyncio.run(scenario(root))
                self.assertIn("example/generate", registry_module.generator_registry._generators)
            finally:
                registry_module.generator_registry = previous_registry
                registry_module.EXTENSIONS_DIR = previous_root

            self.assertTrue(result["success"])
            self.assertFalse((root / "example" / ".modly-incomplete").exists())
            self.assertEqual(list(root.glob(".modly-registration-pending-example-*")), [])
            self.assertEqual(events[-1], {"step": "done", "extensionId": "example"})

    def test_cuda_setup_keeps_pypi_available_for_torch_build_dependencies(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            env = extension_setup_environment("cuda")
        self.assertEqual(env["PIP_EXTRA_INDEX_URL"], "https://pypi.org/simple")
        self.assertEqual(env["MODLY_TORCH_FLAVOR"], "cuda")

    def test_cuda_setup_preserves_an_operator_pip_fallback(self) -> None:
        with patch.dict(os.environ, {"PIP_EXTRA_INDEX_URL": "https://packages.example/simple"}, clear=True):
            env = extension_setup_environment("cuda")
        self.assertEqual(env["PIP_EXTRA_INDEX_URL"], "https://packages.example/simple")

    def test_non_cuda_setup_does_not_add_a_second_package_index(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            env = extension_setup_environment("cpu")
        self.assertNotIn("PIP_EXTRA_INDEX_URL", env)

    def test_failed_setup_streams_output_and_preserves_traceback_details(self) -> None:
        events: list[dict] = []

        async def progress(event: dict) -> None:
            events.append(event)

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(InstallCommandError) as raised:
                asyncio.run(_run_command(
                    [sys.executable, "-c", "print('installing dependency', flush=True); raise RuntimeError('setup exploded')"],
                    Path(tmp),
                    progress,
                    "Setting up Python environment",
                ))

        self.assertEqual(str(raised.exception), "Setting up Python environment failed (exit 1)")
        self.assertIn("installing dependency", raised.exception.details)
        self.assertIn("Traceback (most recent call last):", raised.exception.details)
        self.assertIn("RuntimeError: setup exploded", raised.exception.details)
        self.assertTrue(any(event.get("message") == "installing dependency" for event in events))

    def test_only_canonical_github_repository_urls_are_accepted(self) -> None:
        self.assertEqual(
            parse_github_repository("https://github.com/acme/example.git"),
            ("acme", "example", "https://github.com/acme/example"),
        )
        for invalid in (
            "http://github.com/acme/example",
            "https://evil.example/acme/example",
            "https://github.com/acme/example/issues",
            "https://github.com/acme/example?token=secret",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_github_repository(invalid)

    def test_archive_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                info = tarfile.TarInfo("repo/../../escaped.txt")
                payload = b"nope"
                info.size = len(payload)
                handle.addfile(info, io.BytesIO(payload))
            with self.assertRaises(ValueError):
                extract_archive(archive, root / "output")
            self.assertFalse((root / "escaped.txt").exists())

    def test_manifest_requires_runtime_entry_and_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            extension = Path(tmp)
            (extension / "manifest.json").write_text(json.dumps({
                "id": "example",
                "type": "process",
                "entry": "processor.js",
                "nodes": [{"id": "run"}],
            }))
            with self.assertRaisesRegex(ValueError, "entry file"):
                validate_manifest(extension)
            (extension / "processor.js").write_text("module.exports = {}")
            manifest, entry, is_process, _ = validate_manifest(extension)
            self.assertEqual(manifest["id"], "example")
            self.assertEqual(entry, "processor.js")
            self.assertTrue(is_process)

    def test_recovery_restores_backup_over_incomplete_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "my-extension"
            destination.mkdir()
            (destination / ".modly-incomplete").write_text("installing")
            backup = root / f".modly-backup-my-extension-{'a' * 32}"
            backup.mkdir()
            (backup / "previous.txt").write_text("ok")
            recover_interrupted_installs(root)
            self.assertEqual((destination / "previous.txt").read_text(), "ok")

    def test_recovery_rolls_back_registration_interrupted_after_marker_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "my-extension"
            destination.mkdir()
            (destination / "new.txt").write_text("unvalidated")
            backup = root / f".modly-backup-my-extension-{'a' * 32}"
            backup.mkdir()
            (backup / "previous.txt").write_text("ok")
            state = root / ".modly-registration-pending-my-extension-100"
            state.write_text("{}")

            recover_interrupted_installs(root)

            self.assertEqual((destination / "previous.txt").read_text(), "ok")
            self.assertFalse((destination / "new.txt").exists())
            self.assertFalse(state.exists())

    def test_recovery_removes_unvalidated_fresh_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "my-extension"
            destination.mkdir()
            (destination / "new.txt").write_text("unvalidated")
            state = root / ".modly-registration-pending-my-extension-100"
            state.write_text("{}")

            recover_interrupted_installs(root)

            self.assertFalse(destination.exists())
            self.assertFalse(state.exists())


class BuiltinExtensionSyncTests(unittest.TestCase):
    def test_sync_marks_builtins_and_does_not_overwrite_user_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "extensions"
            source.mkdir()
            builtin = source / "builtin-one"
            builtin.mkdir()
            (builtin / "manifest.json").write_text(json.dumps({
                "id": "builtin-one", "type": "process", "entry": "processor.py",
                "nodes": [{"id": "run"}],
            }))
            (builtin / "processor.py").write_text("pass")
            user = destination / "builtin-one"
            user.mkdir(parents=True)
            (user / "user.txt").write_text("preserve")
            old = os.environ.get("MODLY_BUILTIN_EXTENSIONS_DIR")
            os.environ["MODLY_BUILTIN_EXTENSIONS_DIR"] = str(source)
            try:
                self.assertEqual(sync_builtin_extensions(destination), [])
                self.assertEqual((user / "user.txt").read_text(), "preserve")
                shutil.rmtree(user)
                self.assertEqual(sync_builtin_extensions(destination), ["builtin-one"])
                self.assertTrue((user / BUILTIN_MARKER).is_file())
                self.assertTrue(json.loads((user / "manifest.json").read_text())["builtin"])
            finally:
                if old is None:
                    os.environ.pop("MODLY_BUILTIN_EXTENSIONS_DIR", None)
                else:
                    os.environ["MODLY_BUILTIN_EXTENSIONS_DIR"] = old


class OptionalServerLimitTests(unittest.TestCase):
    def test_limits_are_unlimited_by_default_and_accept_unlimited_aliases(self) -> None:
        for value in (None, "", "0", "none", "unlimited"):
            with self.subTest(value=value):
                environment = {} if value is None else {"MODLY_TEST_LIMIT": value}
                with patch.dict(os.environ, environment, clear=True):
                    self.assertIsNone(optional_env_limit("MODLY_TEST_LIMIT"))

    def test_specific_limit_overrides_global_upload_limit(self) -> None:
        with patch.dict(os.environ, {
            "MODLY_MAX_UPLOAD_BYTES": "100",
            "MODLY_MAX_IMAGE_UPLOAD_BYTES": "25",
        }, clear=True):
            self.assertEqual(
                optional_env_limit("MODLY_MAX_IMAGE_UPLOAD_BYTES", "MODLY_MAX_UPLOAD_BYTES"),
                25,
            )
            self.assertEqual(
                optional_env_limit("MODLY_MAX_ASSET_UPLOAD_BYTES", "MODLY_MAX_UPLOAD_BYTES"),
                100,
            )


if __name__ == "__main__":
    unittest.main()
