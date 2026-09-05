"""Opérations : consultation, filtres, affectation aux enveloppes."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Account, Envelope, Transaction
from ..security import current_user
from ..services.budget import current_period, period_bounds, shift_period
from ..services.categorizer import apply_rules, learn_from_assignment, suggest_pattern
from ..services.importer import detect_transfers
from ..templating import csrf_guard, flash, render

router = APIRouter(dependencies=[Depends(current_user)])

PAGE_SIZE = 60


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


@router.get("/operations", name="transactions_page")
def transactions_page(
    request: Request,
    period: str | None = None,
    account_id: str = "",
    envelope_id: str = "",
    q: str = "",
    kind: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    db: Session = Depends(get_session),
):
    period = period or current_period()
    start, end = period_bounds(period)
    custom_from, custom_to = _parse_date(date_from), _parse_date(date_to)
    if custom_from:
        start = custom_from
    if custom_to:
        end = custom_to

    query = select(Transaction).where(
        Transaction.op_date >= start, Transaction.op_date <= end
    )
    if account_id:
        query = query.where(Transaction.account_id == int(account_id))
    if envelope_id == "none":
        query = query.where(Transaction.envelope_id.is_(None))
    elif envelope_id:
        query = query.where(Transaction.envelope_id == int(envelope_id))
    if kind:
        query = query.where(Transaction.kind == kind)
    if q:
        pattern = f"%{q.strip().upper()}%"
        query = query.where(
            or_(
                func.upper(Transaction.label).like(pattern),
                func.upper(Transaction.raw_label).like(pattern),
                func.upper(Transaction.notes).like(pattern),
            )
        )

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    page = max(page, 1)
    rows = list(
        db.scalars(
            query.order_by(Transaction.op_date.desc(), Transaction.id.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    )
    shown_total = sum(tx.amount_cents for tx in rows if tx.kind != "transfer")

    return render(
        request,
        "transactions.html",
        active="transactions",
        period=period,
        prev_period=shift_period(period, -1),
        next_period=shift_period(period, 1),
        rows=rows,
        total=total,
        page=page,
        pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        shown_total=shown_total,
        accounts=list(db.scalars(select(Account).order_by(Account.position))),
        envelopes=list(
            db.scalars(
                select(Envelope)
                .where(Envelope.archived.is_(False))
                .order_by(Envelope.position, Envelope.name)
            )
        ),
        filters={
            "account_id": account_id,
            "envelope_id": envelope_id,
            "q": q,
            "kind": kind,
            "date_from": date_from,
            "date_to": date_to,
        },
    )


@router.post("/operations/affecter", name="assign_envelope", dependencies=[Depends(csrf_guard)])
def assign_envelope(payload: dict = Body(...), db: Session = Depends(get_session)):
    """Affectation d'une opération, avec apprentissage optionnel d'une règle."""
    try:
        transaction_id = int(payload["transaction_id"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Requête invalide.")

    transaction = db.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(status_code=404, detail="Opération introuvable.")

    raw_envelope = payload.get("envelope_id")
    envelope_id = int(raw_envelope) if raw_envelope not in (None, "", "none") else None
    transaction.envelope_id = envelope_id
    transaction.reviewed = True
    db.commit()

    learned = None
    if envelope_id and payload.get("learn"):
        rule = learn_from_assignment(db, transaction, envelope_id)
        learned = rule.pattern if rule else None

    return JSONResponse(
        {
            "ok": True,
            "envelope_id": envelope_id,
            "learned": learned,
            "suggestion": suggest_pattern(transaction.raw_label or transaction.label),
        }
    )


@router.post("/operations/lot", name="bulk_assign", dependencies=[Depends(csrf_guard)])
async def bulk_assign(request: Request, db: Session = Depends(get_session)):
    """Affectation groupée depuis les cases à cocher de la liste."""
    form = await request.form()
    ids = [int(value) for value in form.getlist("selected") if str(value).isdigit()]
    raw_envelope = str(form.get("envelope_id", ""))
    envelope_id = int(raw_envelope) if raw_envelope and raw_envelope != "none" else None
    learn = bool(form.get("learn"))

    updated = 0
    for transaction_id in ids:
        transaction = db.get(Transaction, transaction_id)
        if transaction is None:
            continue
        transaction.envelope_id = envelope_id
        transaction.reviewed = True
        updated += 1
        if envelope_id and learn:
            learn_from_assignment(db, transaction, envelope_id)
    db.commit()

    response = RedirectResponse(
        request.headers.get("referer") or request.url_for("transactions_page"),
        status_code=303,
    )
    flash(response, f"{updated} opération(s) affectée(s).")
    return response


@router.post("/operations/{transaction_id}/notes", name="transaction_notes", dependencies=[Depends(csrf_guard)])
def transaction_notes(
    request: Request,
    transaction_id: int,
    notes: str = Form(""),
    kind: str = Form(""),
    db: Session = Depends(get_session),
):
    transaction = db.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(status_code=404, detail="Opération introuvable.")
    transaction.notes = notes[:2000]
    if kind in {"expense", "income", "transfer"}:
        transaction.kind = kind
        if kind == "transfer":
            transaction.envelope_id = None
    db.commit()

    response = RedirectResponse(
        request.headers.get("referer") or request.url_for("transactions_page"),
        status_code=303,
    )
    flash(response, "Opération mise à jour.")
    return response


@router.post("/operations/rapprocher", name="detect_transfers_route", dependencies=[Depends(csrf_guard)])
def detect_transfers_route(request: Request, db: Session = Depends(get_session)):
    paired = detect_transfers(db)
    response = RedirectResponse(
        request.headers.get("referer") or request.url_for("transactions_page"),
        status_code=303,
    )
    flash(response, f"{paired} virement(s) interne(s) apparié(s).")
    return response


@router.post("/operations/recategoriser", name="recategorize", dependencies=[Depends(csrf_guard)])
def recategorize(request: Request, db: Session = Depends(get_session)):
    """Repasse les règles sur toutes les opérations encore sans enveloppe."""
    pending = list(
        db.scalars(
            select(Transaction).where(
                Transaction.envelope_id.is_(None), Transaction.reviewed.is_(False)
            )
        )
    )
    count = apply_rules(db, pending)
    response = RedirectResponse(
        request.headers.get("referer") or request.url_for("transactions_page"),
        status_code=303,
    )
    flash(response, f"{count} opération(s) catégorisée(s) par les règles.")
    return response
