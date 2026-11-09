"""
Modly FastAPI backend.
Runs locally within the Electron app to provide AI inference endpoints.
"""
import logging
import os
from pathlib import Path
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from fastapi import FastAPI
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
    # Startup: initialize the registry (instantiates all adapters)
    from services.generator_registry import generator_registry
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
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
