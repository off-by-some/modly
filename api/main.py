"""
Modly FastAPI backend.
Runs locally within the Electron app to provide AI inference endpoints.
"""
import logging
import os
import secrets
from pathlib import Path
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi import HTTPException

from services.stdio_utf8 import ensure_utf8_stdio
ensure_utf8_stdio()  # must run before any print/logging hits the pipe

_workspace_env = os.environ.get("WORKSPACE_DIR")
_log_dir = Path(
    os.environ.get("MODLY_LOG_DIR")
    or (Path(_workspace_env).parent / "logs" if _workspace_env else Path.home() / ".modly" / "logs")
)
_log_dir.mkdir(parents=True, exist_ok=True)
_file_handler = RotatingFileHandler(
    _log_dir / "server.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
for _logger_name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
    _logger = logging.getLogger(_logger_name)
    if not any(isinstance(handler, RotatingFileHandler) for handler in _logger.handlers):
        _logger.addHandler(_file_handler)

from routers import generation, model, optimize, status, settings, extensions, export, workflow_runs, agent, llm, server


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.environ.get("MODLY_SERVER_TOKEN", "").strip():
        if os.environ.get("MODLY_DESKTOP_MANAGED") == "1":
            logging.getLogger("modly.security").info(
                "MODLY_SERVER_TOKEN is not set; the desktop-managed loopback API is unauthenticated."
            )
        else:
            logging.getLogger("modly.security").warning(
                "SECURITY WARNING: MODLY_SERVER_TOKEN is not set. "
                "The Modly API is accepting unauthenticated requests from any client that can reach it."
            )
    # The API owns its extension set. Restore any interrupted transaction and
    # refresh built-ins before the registry sees the directory.
    from services.generator_registry import generator_registry, EXTENSIONS_DIR
    from services.builtin_extensions import sync_builtin_extensions
    from services.extension_installer import recover_interrupted_installs
    if EXTENSIONS_DIR is not None:
        recover_interrupted_installs(EXTENSIONS_DIR)
        if os.environ.get("MODLY_DESKTOP_MANAGED") != "1":
            try:
                sync_builtin_extensions(EXTENSIONS_DIR)
            except Exception:
                # Built-ins are conveniences, not a prerequisite for serving
                # the API. Keep the server online and make the failure visible
                # so a later restart can retry the atomic staging directory.
                logging.getLogger("modly.extensions").exception(
                    "Built-in extension bootstrap failed; continuing without refreshed built-ins."
                )
    generator_registry.initialize()
    yield
    # Shutdown: unload all models and stop the local LLM server
    generator_registry.unload_all()
    from services.llm_server import llama_pool
    llama_pool.unload_all(force=True)


class _StatusFilter(logging.Filter):
    def filter(self, record):
        return "/generate/status/" not in record.getMessage()

logging.getLogger("uvicorn.access").addFilter(_StatusFilter())


app = FastAPI(
    title="Modly API",
    version="0.4.3",
    lifespan=lifespan,
)


@app.middleware("http")
async def require_remote_authentication(request: Request, call_next):
    """Authentication is opt-in through MODLY_SERVER_TOKEN."""
    if request.method == "OPTIONS":
        return await call_next(request)
    expected = os.environ.get("MODLY_SERVER_TOKEN", "").strip()
    if not expected:
        return await call_next(request)
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not supplied or not secrets.compare_digest(supplied, expected):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
            content={"detail": "A valid Modly server token is required."},
        )
    return await call_next(request)


_configured_origins = [
    origin.strip()
    for origin in os.environ.get("MODLY_ALLOWED_ORIGINS", "null,file://,http://localhost:5173").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_configured_origins,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-HF-Token", "X-Modly-Filename"],
    # drei's SplatLoader reads Content-Length to size its buffers; cross-origin
    # JS can only see it when the server explicitly exposes the header.
    expose_headers=["Content-Length"],
)

app.include_router(status.router)
app.include_router(settings.router)
app.include_router(model.router,      prefix="/model")
app.include_router(generation.router, prefix="/generate")
app.include_router(optimize.router,    prefix="/optimize")
app.include_router(extensions.router, prefix="/extensions")
app.include_router(export.router,          prefix="/export")
app.include_router(workflow_runs.router,   prefix="/workflow-runs")
app.include_router(agent.router)
app.include_router(llm.router,             prefix="/llm")
app.include_router(server.router)

# Serve generated files from workspace — dynamic so path changes take effect immediately
@app.get("/workspace/{full_path:path}")
async def serve_workspace_file(full_path: str):
    import services.generator_registry as reg
    workspace_root = reg.WORKSPACE_DIR.resolve()
    file_path = (workspace_root / full_path).resolve()
    if file_path == workspace_root or workspace_root not in file_path.parents:
        raise HTTPException(status_code=400, detail="Workspace path escapes the server workspace")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(file_path))
