"""
Convenience launcher — lets you start the server with:

    python run.py

from inside the backend/ folder, instead of remembering the
`uvicorn app.main:app --reload` command.
"""
import uvicorn

if __name__ == "__main__":
    print("Starting Adinath Agri Services...")
    print("  Once running, open: http://127.0.0.1:8000")
    print("  On first launch, create an administrator in the browser.")
    print("  Press CTRL+C to stop the server.\n")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
