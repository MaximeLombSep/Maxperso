"""Mise en route : un écran par étape, dans l'ordre qui donne un budget juste."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Account, Envelope
from ..security import current_user
from ..services import calibration, categorizer, onboarding
from ..services import savings as savings_service
from ..templating import csrf_guard, flash, path_for, render

router = APIRouter(dependencies=[Depends(current_user)])


@router.get("/mise-en-route", name="onboarding_home")
def onboarding_home(request: Request, db: Session = Depends(get_session)):
    """Reprend là où on s'était arrêté, plutôt qu'au début à chaque fois."""
    return RedirectResponse(
        path_for(request, "onboarding_step", slug=onboarding.first_unfinished(db)),
        status_code=303,
    )


@router.get("/mise-en-route/{slug}", name="onboarding_step")
def onboarding_step(request: Request, slug: str, db: Session = Depends(get_session)):
    if slug not in onboarding.ORDER:
        raise HTTPException(status_code=404, detail="Étape inconnue.")

    steps = onboarding.steps(db)
    current = next(step for step in steps if step.slug == slug)
    done, total = onboarding.progress(db)

    envelopes = list(
        db.scalars(
            select(Envelope)
            .where(Envelope.archived.is_(False))
            .order_by(Envelope.position, Envelope.name)
        )
    )

    return render(
        request,
        "onboarding.html",
        active="onboarding",
        step=current,
        steps=steps,
        position=onboarding.ORDER.index(slug) + 1,
        count=len(onboarding.ORDER),
        done=done,
        total=total,
        previous_slug=onboarding.previous_slug(slug),
        next_slug=onboarding.next_slug(slug),
        envelopes=envelopes,
        income_envelopes=[e for e in envelopes if e.kind == "income"],
        spending_envelopes=[e for e in envelopes if e.kind != "income"],
        savings_hints=savings_service.suggest_savings_accounts(db),
        income_suggestions=categorizer.suggest_rules(db, sign="credit"),
        spending_suggestions=categorizer.suggest_rules(db),
        proposals=calibration.calibrate(db),
        accounts=list(
            db.scalars(
                select(Account).where(Account.archived.is_(False)).order_by(Account.position)
            )
        ),
        has_data=onboarding.has_transactions(db),
    )


@router.post("/mise-en-route/ranger", name="onboarding_dismiss", dependencies=[Depends(csrf_guard)])
def onboarding_dismiss(
    request: Request, hidden: str = Form("on"), db: Session = Depends(get_session)
):
    """Range l'invitation du tableau de bord. Le parcours reste accessible."""
    onboarding.dismiss(db, bool(hidden))
    response = RedirectResponse(path_for(request, "dashboard"), status_code=303)
    flash(
        response,
        "Mise en route rangée. Vous la retrouverez dans « Plus »."
        if hidden
        else "Mise en route réaffichée.",
        "info",
    )
    return response
