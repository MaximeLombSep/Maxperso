"""Écran des enveloppes : dotations, report, reste à budgéter."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Envelope, EnvelopeGroup
from ..security import current_user
from ..services.budget import (
    autofill_month,
    current_period,
    month_summary,
    move_between,
    set_allocation,
    shift_period,
)
from ..services.money import euros_to_cents, format_cents
from ..templating import csrf_guard, flash, render

router = APIRouter(dependencies=[Depends(current_user)])


@router.get("/budget", name="budget_page")
def budget_page(
    request: Request, period: str | None = None, db: Session = Depends(get_session)
):
    period = period or current_period()
    summary = month_summary(db, period)
    return render(
        request,
        "budget.html",
        active="budget",
        period=period,
        prev_period=shift_period(period, -1),
        next_period=shift_period(period, 1),
        summary=summary,
        groups=list(db.scalars(select(EnvelopeGroup).order_by(EnvelopeGroup.position))),
    )


@router.post("/budget/dotation", name="set_allocation", dependencies=[Depends(csrf_guard)])
def update_allocation(payload: dict = Body(...), db: Session = Depends(get_session)):
    """Édition en ligne d'une dotation (appel `fetch` depuis la grille)."""
    try:
        envelope_id = int(payload["envelope_id"])
        period = str(payload["period"])
        cents = euros_to_cents(payload.get("amount", "0"))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Requête invalide.")

    envelope = db.get(Envelope, envelope_id)
    if envelope is None:
        raise HTTPException(status_code=404, detail="Enveloppe introuvable.")
    if envelope.kind == "income":
        raise HTTPException(
            status_code=400,
            detail="Une enveloppe de revenus ne se dote pas : l'argent encaissé "
            "alimente le reste à budgéter.",
        )

    set_allocation(db, envelope_id, period, cents)
    summary = month_summary(db, period)
    state = next(
        (s for s in summary.envelopes if s.envelope.id == envelope_id), None
    )
    return JSONResponse(
        {
            "allocated": format_cents(cents),
            "available": format_cents(state.available if state else 0),
            "available_cents": state.available if state else 0,
            "to_budget": format_cents(summary.to_budget),
            "to_budget_cents": summary.to_budget,
        }
    )


@router.post("/budget/remplir", name="autofill", dependencies=[Depends(csrf_guard)])
def autofill(
    request: Request,
    period: str = Form(...),
    mode: str = Form("suggested"),
    db: Session = Depends(get_session),
):
    filled = autofill_month(db, period, mode)
    response = RedirectResponse(
        request.url_for("budget_page").include_query_params(period=period), status_code=303
    )
    flash(response, f"{filled} enveloppe(s) dotée(s) automatiquement.")
    return response


@router.post("/budget/deplacer", name="move_money", dependencies=[Depends(csrf_guard)])
def move_money(
    request: Request,
    period: str = Form(...),
    source_id: int = Form(...),
    target_id: int = Form(...),
    amount: str = Form(...),
    db: Session = Depends(get_session),
):
    """Reprend de l'argent dans une enveloppe pour en couvrir une autre."""
    response = RedirectResponse(
        request.url_for("budget_page").include_query_params(period=period), status_code=303
    )
    try:
        source, target = move_between(
            db, period, source_id, target_id, euros_to_cents(amount)
        )
    except ValueError as exc:
        flash(response, str(exc), "error")
        return response

    flash(
        response,
        f"{format_cents(euros_to_cents(amount))} déplacés de « {source.name} » "
        f"vers « {target.name} ».",
    )
    return response


@router.get("/enveloppes", name="envelopes_page")
def envelopes_page(request: Request, db: Session = Depends(get_session)):
    return render(
        request,
        "envelopes.html",
        active="envelopes",
        envelopes=list(
            db.scalars(select(Envelope).order_by(Envelope.position, Envelope.name))
        ),
        groups=list(db.scalars(select(EnvelopeGroup).order_by(EnvelopeGroup.position))),
    )


@router.post("/enveloppes", name="envelope_create", dependencies=[Depends(csrf_guard)])
def envelope_create(
    request: Request,
    name: str = Form(...),
    group_id: str = Form(""),
    kind: str = Form("monthly"),
    planned: str = Form("0"),
    target: str = Form("0"),
    target_date: str = Form(""),
    essential: str = Form(""),
    rollover: str = Form("on"),
    color: str = Form("#6366f1"),
    icon: str = Form("•"),
    db: Session = Depends(get_session),
):
    from datetime import datetime

    if db.scalar(select(Envelope).where(Envelope.name == name.strip())):
        response = RedirectResponse(request.url_for("envelopes_page"), status_code=303)
        flash(response, "Une enveloppe porte déjà ce nom.", "error")
        return response

    envelope = Envelope(
        name=name.strip()[:80],
        group_id=int(group_id) if group_id else None,
        kind=kind if kind in {"monthly", "sinking", "income"} else "monthly",
        planned_cents=euros_to_cents(planned),
        target_cents=euros_to_cents(target),
        target_date=(
            datetime.strptime(target_date, "%Y-%m-%d").date() if target_date else None
        ),
        essential=bool(essential),
        rollover=bool(rollover),
        color=color[:7],
        icon=(icon or "•")[:8],
        position=int(db.scalar(select(func.max(Envelope.position))) or 0) + 1,
    )
    db.add(envelope)
    db.commit()

    response = RedirectResponse(request.url_for("envelopes_page"), status_code=303)
    flash(response, f"Enveloppe « {envelope.name} » créée.")
    return response


@router.post(
    "/enveloppes/{envelope_id}", name="envelope_update", dependencies=[Depends(csrf_guard)]
)
def envelope_update(
    request: Request,
    envelope_id: int,
    name: str = Form(...),
    group_id: str = Form(""),
    kind: str = Form("monthly"),
    planned: str = Form("0"),
    target: str = Form("0"),
    target_date: str = Form(""),
    essential: str = Form(""),
    rollover: str = Form(""),
    color: str = Form("#6366f1"),
    icon: str = Form("•"),
    archived: str = Form(""),
    db: Session = Depends(get_session),
):
    from datetime import datetime

    envelope = db.get(Envelope, envelope_id)
    if envelope is None:
        raise HTTPException(status_code=404, detail="Enveloppe introuvable.")

    envelope.name = name.strip()[:80]
    envelope.group_id = int(group_id) if group_id else None
    envelope.kind = kind if kind in {"monthly", "sinking", "income"} else "monthly"
    envelope.planned_cents = euros_to_cents(planned)
    envelope.target_cents = euros_to_cents(target)
    envelope.target_date = (
        datetime.strptime(target_date, "%Y-%m-%d").date() if target_date else None
    )
    envelope.essential = bool(essential)
    envelope.rollover = bool(rollover)
    envelope.color = color[:7]
    envelope.icon = (icon or "•")[:8]
    envelope.archived = bool(archived)
    db.commit()

    response = RedirectResponse(request.url_for("envelopes_page"), status_code=303)
    flash(response, "Enveloppe mise à jour.")
    return response


@router.post("/groupes", name="group_create", dependencies=[Depends(csrf_guard)])
def group_create(
    request: Request, name: str = Form(...), db: Session = Depends(get_session)
):
    if not db.scalar(select(EnvelopeGroup).where(EnvelopeGroup.name == name.strip())):
        last = db.scalar(select(EnvelopeGroup).order_by(EnvelopeGroup.position.desc()))
        db.add(
            EnvelopeGroup(
                name=name.strip()[:80], position=(last.position + 1) if last else 0
            )
        )
        db.commit()
    response = RedirectResponse(request.url_for("envelopes_page"), status_code=303)
    flash(response, "Groupe créé.")
    return response
