import os
import sys
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from .database.migrations import initialize_database
from .routers import auth, products, farmers, billing, dashboard, outstanding, reports, notifications, invoice_documents, accounting


@asynccontextmanager
async def lifespan(_app):
    # Keep database writes at service startup, not while importing the module.
    initialize_database()
    yield

app = FastAPI(
    title="Adinath Agri Services API",
    description="Backend for the Adinath Agri Services billing & inventory platform.",
    version="1.0.0",
    lifespan=lifespan,
)

# Allow the frontend to call the API. Since the frontend is served by this
# same app (see below), this is mostly relevant if you ever split them
# across two different hosts/ports during development.
app.add_middleware(
    CORSMiddleware,
    # Same-origin is the default.  Comma-separated origins can be supplied for
    # a separately hosted frontend, e.g. https://app.example.com.
    allow_origins=[origin.strip() for origin in os.getenv("ADINATH_CORS_ORIGINS", "").split(",") if origin.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request, call_next):
    """Add inexpensive baseline browser protections to every response."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Request-ID"] = request.headers.get("X-Request-ID", str(uuid4()))
    return response

app.include_router(auth.router)
app.include_router(products.router)
app.include_router(farmers.router)
app.include_router(billing.router)
app.include_router(dashboard.router)
app.include_router(outstanding.router)
app.include_router(reports.router)
app.include_router(notifications.router)
app.include_router(invoice_documents.router)
app.include_router(accounting.router)


# ---------------------------------------------------------------------
# FRONTEND — served from the sibling /frontend folder.
# CSS/JS are mounted at /static; the HTML page is served at "/".
# Resolved as an absolute path so it works no matter where you launch
# the app from (double-clicking, a different terminal cwd, etc.).
# ---------------------------------------------------------------------
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(BACKEND_DIR)
# The desktop launcher supplies this path when running from source.  The
# packaged backend uses PyInstaller's temporary resource directory.  Keeping
# this configurable means the same API continues to serve the exact existing
# frontend in browser, development, and installed-desktop modes.
FRONTEND_DIR = os.path.abspath(os.getenv(
    "ADINATH_FRONTEND_DIR",
    os.path.join(getattr(sys, "_MEIPASS", PROJECT_ROOT), "frontend"),
))

if os.path.isdir(FRONTEND_DIR):
    PWA_ASSETS = {
        "manifest.webmanifest": ("manifest.json", "application/manifest+json"),
        "favicon.ico": ("favicon.ico", "image/x-icon"),
        "offline.html": ("offline.html", "text/html"),
    }
    PWA_ICONS = {
        "adinath-agri-logo.svg": "adinath-agri-logo.svg",
        "icon-192.png": "icon-192.png",
        "icon-512.png": "icon-512.png",
        "icon-512-maskable.png": "icon-512-maskable.png",
        "apple-touch-icon.png": "apple-touch-icon.png",
    }

    @app.get("/manifest.webmanifest", include_in_schema=False)
    @app.get("/favicon.ico", include_in_schema=False)
    @app.get("/offline.html", include_in_schema=False)
    def serve_pwa_asset(request: Request):
        """Expose PWA discovery files at the origin root."""
        asset = PWA_ASSETS.get(request.url.path.lstrip("/"))
        if not asset:
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        return FileResponse(os.path.join(FRONTEND_DIR, asset[0]), media_type=asset[1])

    @app.get("/icons/{icon_name}", include_in_schema=False)
    def serve_pwa_icon(icon_name: str):
        """Only serve the allow-listed public PWA icon assets."""
        filename = PWA_ICONS.get(icon_name)
        if not filename:
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        media_type = "image/svg+xml" if filename.endswith(".svg") else "image/png"
        return FileResponse(os.path.join(FRONTEND_DIR, "icons", filename), media_type=media_type)

    app.mount(
        "/static",
        StaticFiles(directory=FRONTEND_DIR),
        name="static",
    )

    @app.get("/service-worker.js", include_in_schema=False)
    def serve_service_worker():
        """Serve the worker at the origin root so it controls the installed app."""
        return FileResponse(
            os.path.join(FRONTEND_DIR, "service-worker.js"),
            media_type="application/javascript",
        )

    @app.get("/", tags=["Frontend"])
    def serve_frontend():
        index_path = os.path.join(FRONTEND_DIR, "index.html")
        if not os.path.isfile(index_path):
            return JSONResponse(
                status_code=500,
                content={"detail": f"index.html not found at {index_path}"},
            )
        return FileResponse(index_path)
else:
    @app.get("/", tags=["Health"])
    def root():
        return {
            "status": "ok",
            "service": "Adinath Agri Services API",
            "note": f"Frontend folder not found at {FRONTEND_DIR} — API-only mode.",
        }


@app.get("/api/health", tags=["Health"])
def health():
    return {"status": "ok", "service": "Adinath Agri Services API"}
