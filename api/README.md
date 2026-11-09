# Modly — FastAPI Backend

Local Python server started and managed by Electron.

## Setup

```bash
cd api
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## Run (development)

```bash
uvicorn main:app --host 127.0.0.1 --port 8765 --reload
```

## Run as a remote server

`MODLY_SERVER_TOKEN` is optional. When it is unset the server accepts
unauthenticated requests and prints a prominent security warning during startup.
Set it to a long random value and enter the same value in Modly's Settings →
Backend panel when the server is reachable by anything you do not fully trust.
Put authenticated APIs behind HTTPS when they cross an untrusted network; the
desktop app requires explicit consent before sending a token over remote HTTP.

Run this from the repository root. Omit the token assignment for an
unauthenticated server.

```bash
MODLY_SERVER_ROOT="$PWD/server-data" && \
mkdir -p "$MODLY_SERVER_ROOT"/{models,workspace,workflows,extensions,llm,logs} && \
env MODLY_SERVER_TOKEN="replace-with-a-long-random-token" \
  MODELS_DIR="$MODLY_SERVER_ROOT/models" \
  WORKSPACE_DIR="$MODLY_SERVER_ROOT/workspace" \
  WORKFLOWS_DIR="$MODLY_SERVER_ROOT/workflows" \
  EXTENSIONS_DIR="$MODLY_SERVER_ROOT/extensions" \
  MODLY_LLM_DIR="$MODLY_SERVER_ROOT/llm" \
  MODLY_LOG_DIR="$MODLY_SERVER_ROOT/logs" \
  api/.venv/bin/python -m uvicorn main:app --app-dir api --host 0.0.0.0 --port 8765
```

At startup the server restores interrupted extension installs and synchronizes
its own built-in workflow extensions. GitHub extensions and catalog models are
downloaded directly by the server; the client uploads only files that originate
on the client, such as generation images, workflow assets, and custom GGUFs.

Do not prefix Uvicorn with `exec` in an interactive SSH shell. Use `tmux`,
`systemd`, or another supervisor when it should survive disconnects.

### Optional limits

Transfers and extension archives are unlimited by default. A positive integer
enables a limit; unset, `0`, `none`, and `unlimited` all mean unlimited.

| Environment variable | Scope |
|----------------------|-------|
| `MODLY_MAX_UPLOAD_BYTES` | Default byte limit for all client uploads |
| `MODLY_MAX_IMAGE_UPLOAD_BYTES` | Generation images; overrides the default |
| `MODLY_MAX_ASSET_UPLOAD_BYTES` | Workspace assets; overrides the default |
| `MODLY_MAX_GGUF_UPLOAD_BYTES` | Custom GGUF files; overrides the default |
| `MODLY_MAX_EXTENSION_ARCHIVE_BYTES` | Downloaded extension archive bytes |
| `MODLY_MAX_EXTENSION_EXTRACTED_BYTES` | Total extracted extension bytes |
| `MODLY_MAX_EXTENSION_ARCHIVE_MEMBERS` | Files in an extension archive |

## Key endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check (used by Electron to detect readiness) |
| GET | `/model/status` | Model download / load status |
| GET | `/model/download` | SSE stream of download progress |
| POST | `/generate/from-image` | Start image-to-3D job |
| POST | `/generate/from-artifact` | Start a typed-artifact model job (`scene` only) |
| GET | `/generate/status/{job_id}` | Poll job status |

## Model

Default: **TripoSR** (`stabilityai/TripoSR`, ~2.4 GB)
Downloaded on first launch to `~/.modly/models/TripoSR/`.
To change model: edit `services/model_manager.py`.
