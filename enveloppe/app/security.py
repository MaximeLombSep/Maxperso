"""Authentification locale et session signée.

Le mot de passe n'est jamais stocké ni journalisé en clair : seul un
condensat scrypt salé est conservé. Le cookie de session est signé
(itsdangerous) avec la clé de `config.settings`, jamais avec une valeur
écrite en dur.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime

from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_session
from .models import User

SESSION_COOKIE = "enveloppe_session"
CSRF_COOKIE = "enveloppe_csrf"

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_serializer = URLSafeTimedSerializer(settings.secret_key, salt="enveloppe-session")


def hash_password(password: str) -> str:
    """Condensat scrypt au format `scrypt$N$r$p$sel$clé` (hexadécimal)."""
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=64,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(key_hex) // 2,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived.hex(), key_hex)


def password_problems(password: str) -> list[str]:
    """Contrôles minimaux : l'application est destinée à être jointe depuis
    l'extérieur, un mot de passe court n'y a pas sa place."""
    problems: list[str] = []
    if len(password) < 12:
        problems.append("12 caractères minimum.")
    if password.lower() in {"motdepasse", "password", "budget", "azertyuiop"}:
        problems.append("Mot de passe trop courant.")
    if password.isdigit():
        problems.append("Pas uniquement des chiffres.")
    return problems


def issue_session(user: User) -> str:
    return _serializer.dumps({"uid": user.id, "u": user.username})


def read_session(token: str) -> dict | None:
    try:
        return _serializer.loads(token, max_age=settings.session_max_age)
    except (BadSignature, SignatureExpired):
        return None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def current_user(
    request: Request, db: Session = Depends(get_session)
) -> User:
    """Dépendance : exige une session valide, sinon 401."""
    token = request.cookies.get(SESSION_COOKIE)
    payload = read_session(token) if token else None
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expirée"
        )
    user = db.get(User, payload.get("uid"))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def has_user(db: Session) -> bool:
    return db.scalar(select(User).limit(1)) is not None


def create_user(db: Session, username: str, password: str) -> User:
    user = User(username=username.strip(), password_hash=hash_password(password))
    db.add(user)
    db.commit()
    return user


def touch_login(db: Session, user: User) -> None:
    user.last_login = datetime.now()
    db.commit()


def secure_cookies_enabled() -> bool:
    """Cookie `Secure` sauf si l'accès est explicitement en HTTP local."""
    return os.environ.get("BUDGET_ALLOW_INSECURE_COOKIES", "").lower() not in {
        "1",
        "true",
        "yes",
    }
