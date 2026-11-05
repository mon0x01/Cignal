from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decode_token
from app.db.models import User
from app.db.session import get_db

bearer = HTTPBearer(auto_error=False)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        claims = decode_token(credentials.credentials)
        if claims.get("type") != "access":
            raise ValueError("Wrong token type")
        user = db.get(User, claims["sub"])
    except Exception:
        user = None
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid or inactive account")
    if settings.mfa_required and not user.mfa_enabled and not request.url.path.startswith("/api/auth/mfa"):
        raise HTTPException(status_code=403, detail={"code": "MFA_SETUP_REQUIRED", "message": "Multi-factor authentication setup is required."})
    return user


def require_roles(*roles: str):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return dependency
