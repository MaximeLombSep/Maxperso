"""Cartes à débit différé : encours, cycles et rapprochement des prélèvements."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Account, Transaction
from ..security import current_user
from ..services import cards as service
from ..services.budget import account_balances
from ..templating import csrf_guard, flash, path_for, render

router = APIRouter(dependencies=[Depends(current_user)])


@router.get("/cartes", name="cards_page")
def cards_page(request: Request, db: Session = Depends(get_session)):
    today = date.today()
    balances = account_balances(db)
    cards = service.deferred_cards(db)

    blocks = []
    for card in cards:
        card_cycles = service.cycles(db, card, today)
        blocks.append(
            {
                "card": card,
                "outstanding": service.outstanding_cents(db, card),
                "next_due": service.next_due(db, card, today),
                "cycles": card_cycles[:12],
                "candidates": {
                    cycle.period: service.candidate_settlements(db, card, cycle)[:6]
                    for cycle in card_cycles
                    if not cycle.settled and cycle.purchases and cycle.is_closed(today)
                },
            }
        )

    return render(
        request,
        "cards.html",
        active="cards",
        blocks=blocks,
        balances=balances,
        projected=service.projected_balances(db, balances),
        unmatched=service.unmatched_settlements(db, today),
        accounts=list(
            db.scalars(
                select(Account).where(Account.archived.is_(False)).order_by(Account.position)
            )
        ),
        today=today,
    )


@router.post("/cartes/rapprocher", name="cards_auto_match", dependencies=[Depends(csrf_guard)])
def cards_auto_match(request: Request, db: Session = Depends(get_session)):
    matched = service.auto_match(db)
    response = RedirectResponse(path_for(request, "cards_page"), status_code=303)
    if matched:
        flash(response, f"{matched} prélèvement(s) de carte rapproché(s).")
    else:
        flash(
            response,
            "Aucune correspondance exacte trouvée : rapprochez à la main ci-dessous.",
            "error",
        )
    return response


@router.post(
    "/cartes/{card_id}/cycles/{period}/rapprocher",
    name="card_link_settlement",
    dependencies=[Depends(csrf_guard)],
)
def card_link_settlement(
    request: Request,
    card_id: int,
    period: str,
    transaction_id: int = Form(...),
    db: Session = Depends(get_session),
):
    card = db.get(Account, card_id)
    settlement = db.get(Transaction, transaction_id)
    if card is None or settlement is None:
        raise HTTPException(status_code=404, detail="Carte ou opération introuvable.")
    if settlement.account_id != card.settlement_account_id:
        raise HTTPException(
            status_code=400,
            detail="Le prélèvement doit appartenir au compte de règlement de la carte.",
        )

    cycle = next((c for c in service.cycles(db, card) if c.period == period), None)
    if cycle is None:
        raise HTTPException(status_code=404, detail="Cycle introuvable.")

    settled = service.link_settlement(db, card, cycle, settlement)
    response = RedirectResponse(path_for(request, "cards_page"), status_code=303)
    flash(
        response,
        f"Cycle {period} soldé : {settled} achat(s) rattaché(s) au prélèvement.",
    )
    return response


@router.post(
    "/cartes/reglements/{transaction_id}/detacher",
    name="card_unlink_settlement",
    dependencies=[Depends(csrf_guard)],
)
def card_unlink_settlement(
    request: Request, transaction_id: int, db: Session = Depends(get_session)
):
    settlement = db.get(Transaction, transaction_id)
    if settlement is None:
        raise HTTPException(status_code=404, detail="Opération introuvable.")

    released = service.unlink_settlement(db, settlement)
    response = RedirectResponse(path_for(request, "cards_page"), status_code=303)
    flash(response, f"Rapprochement défait : {released} achat(s) redeviennent en encours.")
    return response
