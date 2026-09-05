"""Point d'entrée FastAPI : middlewares, démarrage, montage des routes."""

from __future__ import annotations

import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .config import settings
from .db import engine
from .models import Base
from .schema_check import report as report_schema_gaps
from .routers import (
    accounts,
    analytics,
    auth,
    budget,
    cards,
    dashboard,
    imports,
    insurance,
    rules,
    savings,
    transactions,
)
from .templating import path_for
from .security import CSRF_COOKIE, new_csrf_token, secure_cookies_enabled

STATIC_DIR = Path(__file__).parent / "static"

# Sans ce type, le manifeste part en `application/octet-stream` et le
# navigateur refuse d'installer l'application sur l'écran d'accueil.
mimetypes.add_type("application/manifest+json", ".webmanifest")

CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "base-uri 'self'; "
    # L'add-on Home Assistant sert l'interface dans une iframe : le cadrage
    # doit rester possible depuis l'hôte qui proxifie l'application.
    "frame-ancestors 'self' https://*.ui.nabu.casa http://homeassistant.local:8123"
)


class IngressMiddleware(BaseHTTPMiddleware):
    """Marqueur de passage par l'ingress Home Assistant.

    HA proxifie l'add-on sous `/api/hassio_ingress/<jeton>/`, ôte ce préfixe
    du chemin et le transmet dans `X-Ingress-Path`. Le préfixe est relu au
    moment de fabriquer les liens (voir `templating.path_for`).
    """

    async def dispatch(self, request: Request, call_next):
        # Le préfixe sert uniquement à fabriquer les liens : il n'est pas
        # posé dans `root_path`. Starlette retranche en effet `root_path` du
        # chemin pour router les points de montage — or Home Assistant a déjà
        # ôté le préfixe avant de nous transmettre la requête. Le poser
        # faisait chercher les fichiers statiques sous un chemin inexistant,
        # et la feuille de style revenait en 404.
        return await call_next(request)


class SecurityMiddleware(BaseHTTPMiddleware):
    """En-têtes de sécurité et émission du jeton CSRF."""

    async def dispatch(self, request: Request, call_next):
        # Le jeton est décidé AVANT le rendu : sans cela, le tout premier
        # formulaire servi à un navigateur neuf sortirait avec un jeton vide
        # et sa soumission serait rejetée.
        token = request.cookies.get(CSRF_COOKIE) or new_csrf_token()
        request.state.csrf_token = token

        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )

        if request.cookies.get(CSRF_COOKIE) != token:
            response.set_cookie(
                CSRF_COOKIE,
                token,
                httponly=False,  # lu par le script pour les requêtes fetch
                samesite="lax",
                secure=secure_cookies_enabled(),
                path="/",
            )
        return response


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Le schéma est créé au démarrage s'il n'existe pas déjà.

    SQLite est local à l'installation. Les tables manquantes sont créées ;
    une table existante n'est jamais modifiée automatiquement — un écart de
    colonnes est signalé dans les journaux avec le script à exécuter.
    """
    Base.metadata.create_all(bind=engine)
    report_schema_gaps(engine)
    yield


def create_app() -> FastAPI:
    application = FastAPI(
        title="Enveloppe",
        description="Suivi de budget en mode enveloppe, auto-hébergé.",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    application.add_middleware(SecurityMiddleware)
    application.add_middleware(IngressMiddleware)
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    for router in (
        auth.router,
        dashboard.router,
        budget.router,
        transactions.router,
        cards.router,
        imports.router,
        rules.router,
        insurance.router,
        savings.router,
        analytics.router,
        accounts.router,
    ):
        application.include_router(router)

    @application.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException):
        """Une session expirée renvoie l'utilisateur vers la page de connexion."""
        # Une session expirée sur une requête de navigation renvoie vers la
        # page de connexion ; les appels `fetch` de l'interface, eux, doivent
        # recevoir une erreur JSON exploitable.
        accept = request.headers.get("accept", "")
        content_type = request.headers.get("content-type", "")
        wants_json = "application/json" in accept or content_type.startswith(
            "application/json"
        )
        wants_html = not wants_json
        if exc.status_code == 401 and wants_html:
            return RedirectResponse(
                path_for(request, "login_form").include_query_params(
                    next=request.url.path
                ),
                status_code=303,
            )
        if wants_html and exc.status_code in {403, 404}:
            from .templating import render

            response = render(
                request,
                "error.html",
                status_code=exc.status_code,
                detail=exc.detail,
                active="",
            )
            response.status_code = exc.status_code
            return response
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @application.get("/health", include_in_schema=False)
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    return application


app = create_app()
