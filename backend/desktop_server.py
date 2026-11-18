"""Production server entry point used by the Windows desktop application."""
import os
import sys

import uvicorn


def resource_path(*parts: str) -> str:
    """Find PyInstaller bundled files, or source-tree files during development."""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS  # type: ignore[attr-defined]
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


if __name__ == "__main__":
    # main.py reads this before it mounts static files.
    os.environ.setdefault("ADINATH_FRONTEND_DIR", resource_path("frontend"))
    port = int(os.getenv("ADINATH_PORT", "8765"))
    # Import directly rather than through Uvicorn's string importer. This lets
    # PyInstaller detect and bundle every router used by the application.
    from app.main import app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
