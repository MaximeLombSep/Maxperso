"""Règles de catégorisation : consultation et édition."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Envelope, Rule, Transaction
from ..security import current_user
from ..services.categorizer import apply_rules
from ..services.money import euros_to_cents
from ..templating import csrf_guard, flash, path_for, render

router = APIRouter(dependencies=[Depends(current_user)])


@router.get("/regles", name="rules_page")
def rules_page(request: Request, db: Session = Depends(get_session)):
    return render(
        request,
        "rules.html",
        active="rules",
        rules=list(db.scalars(select(Rule).order_by(Rule.priority, Rule.pattern))),
        envelopes=list(
            db.scalars(
                select(Envelope)
                .where(Envelope.archived.is_(False))
                .order_by(Envelope.position, Envelope.name)
            )
        ),
    )


@router.post("/regles", name="rule_create", dependencies=[Depends(csrf_guard)])
def rule_create(
    request: Request,
    pattern: str = Form(...),
    envelope_id: int = Form(...),
    matcher: str = Form("contains"),
    sign: str = Form("any"),
    priority: int = Form(100),
    min_amount: str = Form(""),
    max_amount: str = Form(""),
    db: Session = Depends(get_session),
):
    if db.get(Envelope, envelope_id) is None:
        raise HTTPException(status_code=404, detail="Enveloppe introuvable.")

    db.add(
        Rule(
            pattern=pattern.strip().upper()[:200],
            envelope_id=envelope_id,
            matcher=matcher if matcher in {"contains", "prefix", "regex"} else "contains",
            sign=sign if sign in {"any", "debit", "credit"} else "any",
            priority=priority,
            min_cents=euros_to_cents(min_amount) or None,
            max_cents=euros_to_cents(max_amount) or None,
        )
    )
    db.commit()

    response = RedirectResponse(path_for(request, "rules_page"), status_code=303)
    flash(response, "Règle ajoutée.")
    return response


@router.post("/regles/{rule_id}/supprimer", name="rule_delete", dependencies=[Depends(csrf_guard)])
def rule_delete(request: Request, rule_id: int, db: Session = Depends(get_session)):
    rule = db.get(Rule, rule_id)
    if rule is not None:
        db.delete(rule)
        db.commit()
    response = RedirectResponse(path_for(request, "rules_page"), status_code=303)
    flash(response, "Règle supprimée.")
    return response


@router.post("/regles/{rule_id}/bascule", name="rule_toggle", dependencies=[Depends(csrf_guard)])
def rule_toggle(request: Request, rule_id: int, db: Session = Depends(get_session)):
    rule = db.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Règle introuvable.")
    rule.enabled = not rule.enabled
    db.commit()
    return RedirectResponse(path_for(request, "rules_page"), status_code=303)


@router.post("/regles/appliquer", name="rules_apply", dependencies=[Depends(csrf_guard)])
def rules_apply(
    request: Request, scope: str = Form("pending"), db: Session = Depends(get_session)
):
    """`pending` : opérations sans enveloppe. `all` : tout sauf les affectations manuelles."""
    query = select(Transaction)
    if scope == "pending":
        query = query.where(Transaction.envelope_id.is_(None))
    query = query.where(Transaction.reviewed.is_(False))

    count = apply_rules(db, list(db.scalars(query)), only_uncategorized=(scope == "pending"))
    response = RedirectResponse(path_for(request, "rules_page"), status_code=303)
    flash(response, f"{count} opération(s) recatégorisée(s).")
    return response
