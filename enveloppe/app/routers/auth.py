"""Installation initiale, connexion, déconnexion."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_session
from ..security import (
    SESSION_COOKIE,
    create_user,
    has_user,
    issue_session,
    password_problems,
    secure_cookies_enabled,
    touch_login,
    verify_password,
)
from ..services.seed import bootstrap
from ..templating import csrf_guard, flash, render
from ..models import User
from sqlalchemy import select

router = APIRouter()


def _safe_next(raw: str | None) -> str:
    """N'accepte qu'une redirection interne (pas d'URL absolue)."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


@router.get("/installation", name="setup_form")
def setup_form(request: Request, db: Session = Depends(get_session)):
    if has_user(db):
        return RedirectResponse(request.url_for("login_form"), status_code=303)
    return render(request, "setup.html", active="setup", problems=[])


@router.post("/installation", name="setup_submit", dependencies=[Depends(csrf_guard)])
def setup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_session),
):
    if has_user(db):
        return RedirectResponse(request.url_for("login_form"), status_code=303)

    problems = password_problems(password)
    if password != password_confirm:
        problems.append("Les deux mots de passe diffèrent.")
    if not username.strip():
        problems.append("Nom d'utilisateur requis.")
    if problems:
        return render(request, "setup.html", active="setup", problems=problems)

    user = create_user(db, username, password)
    bootstrap(db)

    response = RedirectResponse(request.url_for("dashboard"), status_code=303)
    _set_session(response, user)
    flash(response, "Installation terminée. Vos enveloppes de départ sont prêtes.")
    return response


@router.get("/connexion", name="login_form")
def login_form(request: Request, db: Session = Depends(get_session)):
    if not has_user(db):
        return RedirectResponse(request.url_for("setup_form"), status_code=303)
    return render(
        request,
        "login.html",
        active="login",
        error=None,
        next=_safe_next(request.query_params.get("next")),
    )


@router.post("/connexion", name="login_submit", dependencies=[Depends(csrf_guard)])
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_session),
):
    user = db.scalar(select(User).where(User.username == username.strip()))
    if user is None or not verify_password(password, user.password_hash):
        return render(
            request,
            "login.html",
            active="login",
            error="Identifiants incorrects.",
            next=_safe_next(next),
        )

    touch_login(db, user)
    response = RedirectResponse(_safe_next(next), status_code=303)
    _set_session(response, user)
    return response


@router.post("/deconnexion", name="logout", dependencies=[Depends(csrf_guard)])
def logout(request: Request):
    response = RedirectResponse(request.url_for("login_form"), status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


def _set_session(response: RedirectResponse, user: User) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(user),
        httponly=True,
        samesite="lax",
        secure=secure_cookies_enabled(),
        path="/",
    )
