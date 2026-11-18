import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Resolve relative to backend so the app and desktop package use the same DB.
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.name == "nt":
    # Browser and Electron share one per-user database on Windows.
    app_data = os.getenv("APPDATA")
    default_db_path = (
        os.path.join(app_data, "adinath-agri-services-desktop", "data", "adinath.db")
        if app_data else os.path.join(BACKEND_DIR, "adinath.db")
    )
else:
    default_db_path = os.path.join(BACKEND_DIR, "adinath.db")
DB_PATH = os.path.abspath(os.getenv("ADINATH_DB_PATH", default_db_path))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 15},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
