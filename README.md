<p align="center">
  <img src="resources/icons/icon.png" width="96" alt="Modly logo" />
</p>

# Modly Relay

**A server-first Modly fork for remote GPU machines and containerized extensions.**

The original Modly desktop mixed UI responsibilities with backend filesystem,
download, extension, and execution concerns. This fork moves those operations
behind the Modly API, making the server authoritative whether the UI uses its
bundled local server or connects to a remote GPU machine.

Extensions installed from a URL can also be built and run in isolated Docker
containers, with NVIDIA Docker runtime and CDI GPU support. In short: keep the UI
on your desktop, run the heavy work wherever the GPU lives, and isolate custom
extension environments from one another.

Modly turns photos into 3D models using open source AI models and runs on Windows,
Linux, and Apple Silicon macOS.

> Created by [Lightning Pixel](https://github.com/lightningpixel)

<p align="center">
  <img src="docs/app-screenshot.png" alt="Modly screenshot" />
</p>

---


## Download

Head to the [Releases](../../releases/latest) page to download the latest installer for Windows, Linux, or Apple Silicon macOS.

Alternatively, you can clone the repository and run the app directly without installing:

```bash
# Windows
launch.bat

# Linux / macOS
./launch.sh
```

---

## What this fork adds

This fork separates the desktop UI from backend execution and adds:

- configurable bundled-local and remote API servers, allowing the UI to drive a
  separate GPU machine;
- server-authoritative models, downloads, extensions, workflows, workspace data,
  logs, RAM reporting, and process execution;
- optional bearer-token authentication;
- server-side extension cloning, setup, validation, repair, and uninstall;
- live extension installation output, retained tracebacks, SSE keep-alives, and
  recoverable server install jobs when the progress connection drops;
- server-owned built-in extension bootstrap;
- optional Docker isolation for URL-installed extensions, including inferred
  Python versions and NVIDIA runtime/CDI GPU support;
- configurable upload and extension archive limits, unlimited by default;
- remote health monitoring, outage notification, and automatic fallback to the
  bundled server without changing or deleting either server's data;
- workflow state fixes that preserve nodes when images or new nodes are added; and
- a macOS package-and-install command that replaces the app bundle while preserving
  user data.

---



## Getting started

### 1. Install JS dependencies

```bash
npm install
```

### 2. Set up Python backend

```bash
cd api
python -m venv .venv
.venv\Scripts\activate     # Windows
source .venv/bin/activate  # Linux / macOS
pip install -r requirements.txt
```

### 3. Run in development

```bash
npm run dev
```

> On Apple Silicon macOS, `npm run package-and-install` builds the current tree and
> replaces `/Applications/Modly.app` while preserving settings, models, extensions,
> workflows, workspace files, and other user data. Use `npm run package:mac` when
> you only need the packaged artifact.

### 4. Test

```bash
npm test
npm run lint
npm run build
```

---

## Backend servers and data ownership

Choose the active server under **Settings → Backend**.

| Mode | API endpoint | Data owner | Compute owner |
|---|---|---|---|
| Bundled server | `http://127.0.0.1:8765` | The desktop-configured local models, extensions, workflows, workspace, agent, and log directories | The desktop machine |
| Remote server | The configured HTTP(S) URL | Directories configured on the remote server | The remote server |

Switching servers changes which catalog and data are displayed; it does not copy,
move, or delete data. Returning to the bundled server restores the same local
directories used before remote support was enabled. When a saved remote server is
unavailable during startup, Modly starts the bundled server before loading the UI
and notifies the user. During a later outage it retries, prompts the user, and can
automatically swap to the bundled server after repeated failures.

Remote paths are server paths. Files that exist only on the desktop, such as an
image selected from a native file dialog or an imported GGUF model, are uploaded
to the selected server. Fetchable resources such as GitHub extension repositories
and Hugging Face model files are downloaded directly by that server.

### Run Modly on a remote server

One of this fork's main features is keeping the desktop UI on one machine while
models, extensions, workflows, downloads, and generation run on a remote GPU
server. On the remote machine, clone this repository and run the following from
its root. The example uses `python3`; set `PYTHON_BIN` if the interpreter has a
different name or path.

```bash
MODLY_SERVER_DATA="${MODLY_SERVER_DATA:-$(pwd)/server-data}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
MODLY_API_PORT="${MODLY_API_PORT:-8765}"

"$PYTHON_BIN" -m venv api/.venv
api/.venv/bin/python -m pip install --upgrade pip
api/.venv/bin/python -m pip install -r api/requirements.txt

mkdir -p \
  "$MODLY_SERVER_DATA/models" \
  "$MODLY_SERVER_DATA/workspace" \
  "$MODLY_SERVER_DATA/workflows" \
  "$MODLY_SERVER_DATA/extensions" \
  "$MODLY_SERVER_DATA/llm" \
  "$MODLY_SERVER_DATA/logs"

(
  cd api
  env \
    MODELS_DIR="$MODLY_SERVER_DATA/models" \
    WORKSPACE_DIR="$MODLY_SERVER_DATA/workspace" \
    WORKFLOWS_DIR="$MODLY_SERVER_DATA/workflows" \
    EXTENSIONS_DIR="$MODLY_SERVER_DATA/extensions" \
    MODLY_LLM_DIR="$MODLY_SERVER_DATA/llm" \
    MODLY_LOG_DIR="$MODLY_SERVER_DATA/logs" \
    MODLY_API_PORT="$MODLY_API_PORT" \
    ./.venv/bin/python -m uvicorn main:app \
      --host 0.0.0.0 \
      --port "$MODLY_API_PORT"
)
```

The server binds every interface in this example. Set a token before starting it
whenever untrusted clients can reach the port:

```bash
export MODLY_SERVER_TOKEN="replace-with-a-long-random-token"
```

`MODLY_SERVER_TOKEN` is optional. When set, every API request must use it as a
bearer token; enter the same value in **Settings → Backend**. When unset, the
server accepts unauthenticated requests. Prefer HTTPS when a token crosses a
network; the UI requires explicit consent before sending credentials over plain
remote HTTP. In the desktop, select **Remote**, enter
`http://<server-address>:8765` (or your HTTPS reverse-proxy URL), and click
**Test & connect**.

### Server environment variables

| Variable | Purpose | Default |
|---|---|---|
| `MODELS_DIR` | Downloaded model weights and extension runtime caches | `~/.modly/models` |
| `WORKSPACE_DIR` | Generated assets, exports, workflow inputs, and temporary files | `~/.modly/workspace` |
| `WORKFLOWS_DIR` | Saved workflow JSON documents | Sibling `workflows` directory, or `~/.modly/workflows` |
| `EXTENSIONS_DIR` | Server-owned built-in and user-installed extensions | Must be set for extension management |
| `MODLY_LLM_DIR` | Local agent engine, GGUF models, configuration, and related state | `~/.modly/llm` |
| `MODLY_LOG_DIR` | Rotating `server.log` location | Sibling `logs` directory, or `~/.modly/logs` |
| `MODLY_SERVER_TOKEN` | Optional bearer token required by every non-`OPTIONS` request | Authentication disabled |
| `MODLY_ALLOWED_ORIGINS` | Comma-separated CORS origins | Desktop/file and local development origins |
| `HF_TOKEN` / `HUGGING_FACE_HUB_TOKEN` | Optional Hugging Face credential inherited by downloads and extension runtimes | Unset |
| `MODLY_API_PORT` | API port advertised to Docker extension containers | `8765` |
| `MODLY_CONTAINER_API_URL` | Explicit API URL used from inside extension containers | Derived from platform and `MODLY_API_PORT` |
| `MODLY_EXTENSION_SETUP_TIMEOUT` | Maximum extension setup time in seconds | `1800` |
| `MODLY_EXTENSION_PYPI_INDEX_URL` | PyPI fallback added to CUDA extension setup when no extra index is already configured | `https://pypi.org/simple` |
| `MODLY_MAX_UPLOAD_BYTES` | Fallback limit for client-originated uploads | Unlimited |
| `MODLY_MAX_IMAGE_UPLOAD_BYTES` | Image upload override | `MODLY_MAX_UPLOAD_BYTES` |
| `MODLY_MAX_ASSET_UPLOAD_BYTES` | Desktop asset staging override | `MODLY_MAX_UPLOAD_BYTES` |
| `MODLY_MAX_GGUF_UPLOAD_BYTES` | GGUF upload override | `MODLY_MAX_UPLOAD_BYTES` |
| `MODLY_MAX_EXTENSION_ARCHIVE_BYTES` | Compressed GitHub extension archive limit | Unlimited |
| `MODLY_MAX_EXTENSION_EXTRACTED_BYTES` | Extracted extension size limit | Unlimited |
| `MODLY_MAX_EXTENSION_ARCHIVE_MEMBERS` | Extension archive member-count limit | Unlimited |

For byte/count limits, unset, `0`, `none`, and `unlimited` all mean unlimited.

### Docker extension runtime

Enable **Use Docker** under **Settings → Backend → Extension runtime**. Capability
detection runs on the selected server, so a remote connection inspects remote Docker
rather than the desktop installation.

- Built-in extensions remain visible in either mode.
- Custom extensions installed without Docker are hidden, not deleted, while Docker
  mode is selected. Reinstall their GitHub URL to create a containerized copy.
- URL-installed Docker extensions are built and invoked on the server. Modly infers
  Python from the extension manifest, wheelhouse metadata, or standard Python package
  metadata, then falls back to the server Python version.
- Containers receive controlled model, workspace, API, and persistent cache mounts.
  On Linux they run with the server user's UID/GID so generated files do not become
  root-owned.
- NVIDIA GPU access supports both the registered NVIDIA Docker runtime (`--gpus all`)
  and Docker CDI (`--device nvidia.com/gpu=all`). Settings distinguishes a missing
  toolkit from a toolkit that is installed but not configured for the Docker daemon.

To configure NVIDIA Container Toolkit with Docker's NVIDIA runtime:

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Useful diagnostics are:

```bash
docker info --format '{{json .Runtimes}}'
nvidia-ctk --version
nvidia-ctk cdi list
docker info --format '{{json .CDISpecDirs}}'
```

---

## Platform notes

- AMD GPUs are supported through ROCm on Linux and Windows: a Radeon card is detected
  automatically and extensions are steered to ROCm PyTorch wheels, with no ROCm install
  required. See [docs/running-on-amd-rocm.md](docs/running-on-amd-rocm.md).
- macOS support targets Apple Silicon only.
- macOS uses native window controls. Windows and Linux keep the existing custom controls.
- The top bar includes a live RAM indicator sourced from the selected server.
- Workflow wiring is validated before run; invalid graphs stay in place and surface inline/toast warnings instead of dropping the current mesh view.
- Imported meshes can be smoothed and decimated in-app; optimized results are written back into the workspace.

---

## Extension system

Modly supports external model and process extensions. Each extension is a GitHub repository containing a `manifest.json` plus the runtime entry files required by its type.

The selected server owns extension discovery and lifecycle operations. Installing
from a GitHub URL sends the URL to that server; the server downloads and validates
the repository, prepares its system or Docker runtime, registers it atomically, and
then refreshes the catalog. The desktop does not clone a remote server's extension
locally.

Installation output streams back to the Models page and is also written to the
server log. The UI retains recent output and the full traceback when setup fails.
Long-running installs have a server job ID: if the SSE connection drops during a
large dependency download, the install continues and the client recovers progress
from the server instead of reporting a false terminal failure. Interrupted server
transactions are recovered or rolled back during startup, and **Repair** reruns setup
and runtime validation on the selected server.

Built-in workflow extensions are bootstrapped by both bundled and standalone
servers. Local-folder linking remains a bundled-server developer feature because a
remote client must not name arbitrary host paths on the server.

### Official extensions

| Extension | Model | URL |
|-----------|-------|-----|
| [modly-hunyuan3d-mini-extension](https://github.com/lightningpixel/modly-hunyuan3d-mini-extension) | Hunyuan3D 2 Mini | https://github.com/lightningpixel/modly-hunyuan3d-mini-extension |
| [modly-hunyuan3d-mini-turbo-extension](https://github.com/lightningpixel/modly-hunyuan3d-mini-turbo-extension) | Hunyuan3D 2 Mini Turbo | https://github.com/lightningpixel/modly-hunyuan3d-mini-turbo-extension |
| [modly-hunyuan3d-mini-fast-extension](https://github.com/lightningpixel/modly-hunyuan3d-mini-fast-extension) | Hunyuan3D 2 Mini Fast | https://github.com/lightningpixel/modly-hunyuan3d-mini-fast-extension |
| [modly-triposg-extension](https://github.com/lightningpixel/modly-triposg-extension) | TripoSG | https://github.com/lightningpixel/modly-triposg-extension |
| [modly-trellis2-gguf-extension](https://github.com/lightningpixel/modly-trellis2-gguf-extension) | Trellis2 GGUF | https://github.com/lightningpixel/modly-trellis2-gguf-extension |

### How to install an extension

**1.** Go to the **Models** page and click **Install from GitHub**.

![Install from GitHub](docs/install-from-github.png)

**2.** Enter the HTTPS URL of the extension repository and confirm.

![Enter extension URL](docs/install-extension.png)

**3.** If the extension exposes model nodes, download the model or one of its variants. Process extensions are ready once installation and setup complete.

![Install models](docs/install-models.png)

### Multiple Hugging Face repositories per model node

A model node whose weights are split across repositories can declare
`model_sources`. Modly validates every source, downloads them sequentially in
one Models-page action, and considers the node installed only when every
declared check exists.

```json
{
  "id": "generate",
  "model_sources": [
    {
      "id": "primary",
      "provider": "huggingface",
      "repo_id": "org/main-model",
      "destination": ".",
      "checks": ["model.safetensors"]
    },
    {
      "id": "encoder",
      "provider": "huggingface",
      "repo_id": "org/encoder",
      "revision": "v1.0",
      "destination": "auxiliary/encoder",
      "include_prefixes": ["config.json", "model.safetensors"],
      "checks": ["config.json", "model.safetensors"]
    }
  ]
}
```

`destination`, filters, and checks use safe POSIX paths relative to the node's
model directory. Every check must name a regular, non-empty file included by
that source's filters; invalid plans fail before any file is downloaded. Pin a
tag or commit in `revision` when reproducible weights are required. The only
supported provider is `huggingface`. Existing nodes that use `hf_repo`,
`download_check`, `hf_include_prefixes`, and `hf_skip_prefixes` keep their
original behavior.

### Shared weights inside one model extension

Multi-node model extensions can declare extension-scoped `weight_groups` and
reference them from any sibling node. Shared files are downloaded once under
`<models-dir>/<extension-id>/_shared/<group-id>`, while node-specific
`model_sources` stay under the node's existing model directory.

```json
{
  "id": "pixal3d",
  "type": "model",
  "weight_groups": [
    {
      "id": "pixal3d-base",
      "model_sources": [
        {
          "id": "base",
          "provider": "huggingface",
          "repo_id": "TencentARC/Pixal3D",
          "revision": "<pinned-revision>",
          "destination": ".",
          "checks": ["pipeline.json"]
        }
      ]
    }
  ],
  "nodes": [
    {
      "id": "generate",
      "weight_groups": ["pixal3d-base"]
    },
    {
      "id": "worldsculpt",
      "weight_groups": ["pixal3d-base"],
      "model_sources": [
        {
          "id": "adapter",
          "provider": "huggingface",
          "repo_id": "AlayaLab/WorldSculpt",
          "revision": "<pinned-revision>",
          "destination": ".",
          "checks": ["model.safetensors"]
        }
      ]
    }
  ]
}
```

At runtime, `MODEL_DIR` remains the selected node's private directory.
Subprocess extensions also receive `MODEL_ID`, `MODEL_NODE_ID`, and a JSON
`SHARED_MODEL_DIRS` map in their environment. Both direct and subprocess generator
instances receive `MODEL_ID`, `MODEL_NODE_ID`, and the resolved mapping in
`shared_model_dirs` before `load()`. Direct generators use these instance attributes,
not process-global environment variables, to distinguish sibling nodes.
Shared groups are installed through their dependent nodes; the drawer exposes
shared-group status and explicit removal. Removing private node data never removes a shared group;
shared-group removal is a separate action that identifies every affected node.

### Separately installable weight variants

A model node that publishes the same weights in several variants (quantizations,
precisions…) can declare `weight_variants` next to `hf_repo`. The Extensions page
lists every variant under the node, and each one is downloaded or deleted on its own.

```json
{
  "id": "generate",
  "hf_repo": "org/model-gguf",
  "download_check": "pipeline.json",
  "hf_include_prefixes": ["pipeline.json", "encoder/", "dit/"],
  "params_schema": [
    {
      "id": "quant",
      "label": "Quantization",
      "type": "select",
      "default": "Q5_K_M",
      "options": [
        { "value": "Q4_K_M", "label": "Q4_K_M" },
        { "value": "Q5_K_M", "label": "Q5_K_M" }
      ]
    }
  ],
  "weight_variants": {
    "param": "quant",
    "default": "Q5_K_M",
    "options": [
      {
        "id": "Q4_K_M",
        "label": "Q4_K_M",
        "size_gb": 2.4,
        "vram_gb": 6,
        "include_prefixes": ["dit/model_Q4_K_M.gguf"],
        "checks": ["dit/model_Q4_K_M.gguf"]
      },
      {
        "id": "Q5_K_M",
        "include_prefixes": ["dit/model_Q5_K_M.gguf"],
        "checks": ["dit/model_Q5_K_M.gguf"]
      }
    ]
  }
}
```

- `param` names the `params_schema` select whose values are the variant ids. That
  param must exist on the node (or on the extension, as its fallback), and when it
  declares `options` they must cover every variant id.
- `size_gb` (download size) and `vram_gb` (approximate VRAM the variant needs) are
  optional positive numbers, shown next to the variant when present.
- Every install downloads the shared files (`hf_include_prefixes`, with every
  variant's files excluded automatically) plus one variant: the one asked for, or the
  `default` one — the first option when `default` is omitted. Files already complete
  on disk are skipped, so adding a second variant only fetches that variant.
  Only declared variants are excluded from the shared pass: keep
  `hf_include_prefixes` narrow enough that a variant the repository publishes but
  the manifest does not declare (e.g. an extra `dit/model_Q8_0.gguf`) is not
  downloaded with every install.
- A variant is installed when all of its `checks` exist; the node is installed once
  its `download_check` and at least one variant are present.
- Generation fails with an explicit message when the selected variant is not
  installed, and the node's selector labels those options `(not installed)`.
- `include_prefixes` and `checks` are safe POSIX paths relative to the node's model
  directory. Prefixes of two variants cannot overlap, `download_check` stays outside
  every variant, and `weight_variants` cannot be combined with `model_sources`.

---

## Workflows
Start with a basic workflow first. For example, on the "Workflows" tab, try: Image -> Generate Mesh -> Add to Scene. Make sure there is a connection between each of the steps. Go to the "Generate" tab, make sure the workflow is selected, then click on "Generate 3D Model". Click on "Settings/Logs/Errors" to see any issues.

Model extensions may also declare `scene` as a node input or output. A scene is
a workspace directory containing `scene-manifest.json` with schema
`modly.scene-manifest.v1`; it is not an arbitrary JSON file. Use the **Load
Scene** workflow node to select and validate an existing scene directory.
Scene-capable generators implement `generate_artifact(input_kind,
artifact_path, ...)`; legacy image generators and `POST /generate/from-image`
remain unchanged. The generic `POST /generate/from-artifact` boundary currently
accepts only `scene`, leaving future artifact kinds to separate reviewed changes.
For this first contract, `scene` is model-only and must be declared as the single
`input` value (not inside `inputs`); process and mixed-input scene nodes are rejected.
Model nodes may still accept multiple images and produce a scene.


## Modly CLI

Agents and scripts can call a running Modly desktop app without using the UI via the stdlib-only CLI. The CLI is a thin helper over Modly's canonical automation concepts and keeps final machine-readable JSON on stdout:

```bash
python tools/modly-cli/agent.py health
python tools/modly-cli/agent.py model list
python tools/modly-cli/agent.py workflow-run status <run_id>
python tools/modly-cli/agent.py generate --image ./input.png --output ./export.glb
```

Canonical commands are `health`, `model`, `workflow-run`, `capability`, and `process-run`. The friendly `generate` command starts `POST /workflow-runs/from-image`, polls the returned run, exports the final mesh when requested, and includes recovery metadata such as `workflow-run status ...` and `workflow-run cancel ...` in the JSON response.

Compatibility and helper surfaces are intentionally separated: `legacy` wraps old `/generate/*` job endpoints, `dev serve-api` / `dev ensure-server` start only the FastAPI backend and do not prove Electron/Desktop bridge readiness, and `experimental comfy-image` / `experimental generate-from-workflow` are external ComfyUI orchestration helpers rather than the canonical Modly agent contract. Hidden helper aliases such as `status`, `export`, and `batch` remain parseable for scripts, but they are not presented as canonical root commands.

`experimental generate-from-workflow --workflow <name> --output <path>` treats `--output` as the final artifact location. When the ComfyUI workflow produces a downloadable 3D asset, the CLI downloads it directly; image-only workflows remain a compatibility path through Modly image-to-3D generation.

See `tools/modly-cli/SKILL.md` for the agent workflow and output contract.

---

### Community

Join the [Discord server](https://discord.gg/BvjDCvS3yr) to stay up to date with the latest news, report bugs, and share feedback.

---

## Sponsors

<p align="center">
  Thanks to our early sponsors for believing in Modly and helping make local AI 3D generation more accessible.
</p>

<p align="center">
  <kbd>
    <img src="https://images.weserv.nl/?url=github.com/DrHepa.png&w=96&h=96&fit=cover&mask=circle" width="40" height="40" alt="DrHepa" />
    <br />
    <sub><a href="https://github.com/DrHepa">DrHepa</a></sub>
  </kbd>
  &nbsp;&nbsp;
  <kbd>
    <img src="https://images.weserv.nl/?url=github.com/benjapenjamin.png&w=96&h=96&fit=cover&mask=circle" width="40" height="40" alt="benjapenjamin" />
    <br />
    <sub><a href="https://github.com/benjapenjamin">benjapenjamin</a></sub>
  </kbd>
  &nbsp;&nbsp;
  <kbd>
    <img src="https://images.weserv.nl/?url=github.com/iammojogo-sudo.png&w=96&h=96&fit=cover&mask=circle" width="40" height="40" alt="iammojogo-sudo" />
    <br />
    <sub><a href="https://github.com/iammojogo-sudo">iammojogo-sudo</a></sub>
  </kbd>
</p>

---

## License

MIT License — see [LICENSE](LICENSE) for details.

**If you fork this project and build your own app from it, you must credit the original project and its creator:**

> Based on [Modly](https://github.com/lightningpixel/modly) by [Lightning Pixel](https://github.com/lightningpixel)

This is a requirement of the MIT license attribution clause. Please keep this credit visible in your app's UI or documentation.
