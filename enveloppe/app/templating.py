"""Environnement de rendu : filtres, contexte commun, messages flash."""

from __future__ import annotations

import hmac
import json
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from starlette.datastructures import URL
from starlette.responses import Response

from . import __version__
from .config import settings
from .security import CSRF_COOKIE, login_required
from .services.budget import current_period, period_label
from .services.money import format_cents, format_cents_short

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

FLASH_COOKIE = "enveloppe_flash"


def path_for(request: Request, name: str, **path_params) -> URL:
    """URL racine-relative d'une route : « /budget », jamais « http://hôte/budget ».

    Deux corrections en une :

    - **l'hôte** — derrière l'ingress, l'application est jointe sur son adresse
      interne (`172.30.x.x:8099`) alors que le navigateur est sur le domaine de
      Home Assistant. Une URL absolue porterait un hôte injoignable ;
    - **le préfixe** — Home Assistant ôte `/api/hassio_ingress/<jeton>` du
      chemin avant de nous transmettre la requête, mais le navigateur, lui, en
      a besoin. Il est relu dans l'en-tête et rajouté ici, sans jamais toucher
      au routage interne.
    """
    absolute = request.url_for(name, **path_params)
    prefix = request.headers.get("X-Ingress-Path", "").rstrip("/")
    relative = f"{prefix}{absolute.path}"
    if absolute.query:
        relative = f"{relative}?{absolute.query}"
    return URL(relative)


@pass_context
def _url_for(context, name: str, /, **path_params) -> URL:
    return path_for(context["request"], name, **path_params)


def _date_fr(value: date | datetime | None, fmt: str = "%d/%m/%Y") -> str:
    if value is None:
        return "—"
    return value.strftime(fmt)


def _decimal_fr(value: float | None, digits: int = 1) -> str:
    """Décimale à la française : « 1,9 » et non « 1.9 »."""
    if value is None:
        return "—"
    return f"{value:.{digits}f}".replace(".", ",")


def _percent(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.{digits}f} %".replace(".", ",")


templates.env.filters["money"] = format_cents
templates.env.filters["money_short"] = format_cents_short
templates.env.filters["date_fr"] = _date_fr
templates.env.filters["percent"] = _percent
templates.env.filters["decimal_fr"] = _decimal_fr
# Remplace le `url_for` de Starlette, qui produit des URLs absolues.
templates.env.globals["url_for"] = _url_for
templates.env.globals["period_label"] = period_label
templates.env.globals["currency"] = settings.currency
templates.env.globals["today"] = date.today


def flash(response: Response, message: str, level: str = "success") -> None:
    """Message affiché au prochain rendu (cookie éphémère, non sensible)."""
    payload = quote(json.dumps({"m": message[:300], "l": level}))
    response.set_cookie(
        FLASH_COOKIE, payload, max_age=30, httponly=True, samesite="lax", path="/"
    )


def _read_flash(request: Request) -> dict | None:
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return None
    try:
        return json.loads(unquote(raw))
    except (ValueError, TypeError):
        return None


def render(request: Request, template: str, **context) -> Response:
    """Rendu d'une page avec le contexte partagé par toutes les vues."""
    base = {
        "version": __version__,
        "login_required": login_required(request)
        and not getattr(request.state, "auth_bypassed", False),
        "password_missing": getattr(request.state, "password_missing", False),
        # Posé par SecurityMiddleware, y compris lors de la toute première
        # requête d'un navigateur qui n'a pas encore le cookie.
        "csrf_token": getattr(request.state, "csrf_token", "")
        or request.cookies.get(CSRF_COOKIE, ""),
        "flash": _read_flash(request),
        "active": context.pop("active", ""),
        "period": context.pop("period", current_period()),
    }
    base.update(context)
    response = templates.TemplateResponse(request, template, base)
    if base["flash"]:
        response.delete_cookie(FLASH_COOKIE, path="/")
    return response


async def csrf_guard(request: Request) -> None:
    """Protection CSRF par double soumission, sur toute requête mutante."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return

    cookie = request.cookies.get(CSRF_COOKIE, "")
    supplied = request.headers.get("X-CSRF-Token", "")
    content_type = request.headers.get("content-type", "")
    if not supplied and (
        content_type.startswith("application/x-www-form-urlencoded")
        or content_type.startswith("multipart/form-data")
    ):
        form = await request.form()
        supplied = str(form.get("csrf_token", ""))

    if not cookie or not supplied or not hmac.compare_digest(cookie, supplied):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Jeton CSRF absent ou invalide — rechargez la page.",
        )
