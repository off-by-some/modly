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

```bash
MODLY_SERVER_TOKEN="replace-with-a-long-random-token" \
MODELS_DIR="$PWD/server-data/models" \
WORKSPACE_DIR="$PWD/server-data/workspace" \
EXTENSIONS_DIR="$PWD/server-data/extensions" \
MODLY_LLM_DIR="$PWD/server-data/llm" \
uvicorn main:app --host 0.0.0.0 --port 8765
```

At startup the server restores interrupted extension installs and synchronizes
its own built-in workflow extensions. GitHub extensions and catalog models are
downloaded directly by the server; the client uploads only files that originate
on the client, such as generation images, workflow assets, and custom GGUFs.

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
