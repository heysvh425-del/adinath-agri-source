from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from .. import models, schemas, auth
from ..database import get_db

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/login", response_model=schemas.Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """Login with username + password (form-encoded, standard OAuth2 password flow).
    Works directly with the FastAPI /docs 'Authorize' button."""
    user = auth.authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = auth.create_access_token({"sub": user.username, "role": user.role.value})
    return schemas.Token(access_token=token, user=user)


@router.get("/setup-status")
def setup_status(db: Session = Depends(get_db)):
    """Expose whether first-launch administrator setup is still required."""
    return {"setup_required": db.query(models.User).count() == 0}


@router.post("/setup", response_model=schemas.UserOut, status_code=201)
def create_initial_administrator(
    payload: schemas.InitialAdminCreate,
    db: Session = Depends(get_db),
):
    """Create the sole unauthenticated user allowed: the first administrator."""
    if db.query(models.User).count() != 0:
        raise HTTPException(status_code=403, detail="Initial setup has already been completed")
    user = models.User(
        name=payload.name,
        username=payload.username,
        password_hash=auth.hash_password(payload.password),
        role=models.RoleEnum.admin,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/register", response_model=schemas.UserOut, status_code=201)
def register(
    payload: schemas.UserCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_roles("admin")),
):
    """Create a new staff/admin account. Admin-only."""
    existing = db.query(models.User).filter(models.User.username == payload.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already taken")
    user = models.User(
        name=payload.name,
        username=payload.username,
        password_hash=auth.hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get("/me", response_model=schemas.UserOut)
def me(current_user: models.User = Depends(auth.get_current_user)):
    return current_user
