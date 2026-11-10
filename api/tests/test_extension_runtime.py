import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.extension_runtime import (
    docker_capabilities,
    docker_image_name,
    docker_run_prefix,
    dockerfile_for_extension,
    extension_is_visible,
    infer_python_version,
    recommended_cuda_lane,
    set_docker_enabled,
    write_extension_runtime,
)


class ExtensionRuntimeTests(unittest.TestCase):
    def test_runtime_modes_hide_only_non_matching_custom_extensions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system = root / "system-extension"
            docker = root / "docker-extension"
            builtin = root / "builtin-extension"
            for path in (system, docker, builtin):
                path.mkdir()
            write_extension_runtime(system, {"kind": "system"})
            write_extension_runtime(docker, {"kind": "docker", "image": "test:1"})
            (builtin / ".modly-builtin").write_text("builtin\n", encoding="utf-8")

            self.assertTrue(extension_is_visible(root, system))
            self.assertFalse(extension_is_visible(root, docker))
            self.assertTrue(extension_is_visible(root, builtin))

            set_docker_enabled(root, True)
            self.assertFalse(extension_is_visible(root, system))
            self.assertTrue(extension_is_visible(root, docker))
            self.assertTrue(extension_is_visible(root, builtin))

    def test_python_version_prefers_manifest_then_wheelhouse(self):
        with tempfile.TemporaryDirectory() as temporary:
            extension = Path(temporary)
            self.assertEqual(
                infer_python_version(extension, {"runtime": {"python": "3.11"}}),
                "3.11",
            )
            (extension / "wheelhouse.manifest.json").write_text(json.dumps({
                "assets": [
                    {"selectors": {"os": "linux", "python_tag": "cp312"}},
                    {"selectors": {"os": "darwin", "python_tag": "cp313"}},
                ],
            }), encoding="utf-8")
            self.assertEqual(infer_python_version(extension, {}), "3.12")

    def test_python_version_uses_standard_metadata_without_wheelhouse(self):
        with tempfile.TemporaryDirectory() as temporary:
            extension = Path(temporary)
            (extension / "pyproject.toml").write_text(
                '[project]\nrequires-python = ">=3.11,<3.13"\n', encoding="utf-8"
            )
            self.assertEqual(infer_python_version(extension, {}), "3.11")

    def test_dockerfile_builds_typescript_and_cuda_setup_inside_the_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            extension = Path(temporary)
            (extension / "setup.py").write_text("", encoding="utf-8")
            (extension / "package.json").write_text("{}", encoding="utf-8")
            dockerfile = dockerfile_for_extension(
                extension,
                {"id": "example", "type": "process"},
                "3.12",
                86,
                124,
                "processor.ts",
            )
            self.assertIn("FROM python:3.12-slim-bookworm", dockerfile)
            self.assertIn("PIP_EXTRA_INDEX_URL=https://pypi.org/simple", dockerfile)
            self.assertIn("npm install --omit=dev", dockerfile)
            self.assertIn("esbuild", dockerfile)

    def test_cuda_lane_preserves_setup_contract_and_image_tags_are_transactional(self):
        with tempfile.TemporaryDirectory() as temporary:
            extension = Path(temporary)
            (extension / "manifest.json").write_text("{}", encoding="utf-8")
            manifest = {"id": "example"}
            first = docker_image_name(extension, manifest, "a" * 32)
            second = docker_image_name(extension, manifest, "b" * 32)
            self.assertNotEqual(first, second)
            self.assertEqual(recommended_cuda_lane(86, 128), 124)
            self.assertEqual(recommended_cuda_lane(120, 128), 128)
            self.assertEqual(recommended_cuda_lane(0, "12.6"), 126)

    @patch("services.extension_runtime.platform.system", return_value="Linux")
    @patch("services.extension_runtime.shutil.which", return_value="/usr/bin/docker")
    def test_docker_command_does_not_put_credentials_in_arguments(self, _which, _system):
        command = docker_run_prefix(
            "modly-extension-example:0123456789abcdef",
            name="modly-test",
            models_dir=Path("/tmp/models"),
            workspace_dir=Path("/tmp/workspace"),
            api_dir=Path("/tmp/api"),
            cache_dir=Path("/tmp"),
            nvidia=True,
            environment={"MODLY_SERVER_TOKEN": None},
        )
        self.assertIn("MODLY_SERVER_TOKEN", command)
        self.assertFalse(any(item.startswith("MODLY_SERVER_TOKEN=") for item in command))
        self.assertIn("host", command)

    @patch("services.extension_runtime.shutil.which")
    @patch("services.extension_runtime._run_probe")
    def test_docker_capability_requires_a_working_daemon(self, run_probe, which):
        with tempfile.TemporaryDirectory() as temporary:
            which.side_effect = lambda name: "/usr/bin/docker" if name == "docker" else None
            run_probe.return_value = (False, "cannot connect")
            status = docker_capabilities(Path(temporary))
            self.assertFalse(status["dockerAvailable"])
            self.assertIn("daemon", status["reason"])


if __name__ == "__main__":
    unittest.main()
