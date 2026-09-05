"""Tableau de bord : l'état du mois en un écran."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Account, Transaction
from ..security import current_user
from ..services import analytics, calibration, cards, categorizer, insurance, savings
from ..services.budget import (
    account_balances,
    current_period,
    month_summary,
    shift_period,
)
from ..templating import render

router = APIRouter(dependencies=[Depends(current_user)])


@router.get("/", name="dashboard")
def dashboard(
    request: Request,
    period: str | None = None,
    db: Session = Depends(get_session),
):
    period = period or current_period()
    # Le mois en cours hérite du budget de référence s'il n'a jamais été doté.
    provisioned = calibration.ensure_provisioned(db, period)
    summary = month_summary(db, period)
    balances = account_balances(db)
    accounts = list(
        db.scalars(
            select(Account).where(Account.archived.is_(False)).order_by(Account.position)
        )
    )

    series = analytics.monthly_series(db, months=6, end=period)
    overspent = sorted(
        [state for state in summary.envelopes if state.overspent],
        key=lambda state: state.available,
    )[:5]
    biggest = sorted(
        [state for state in summary.envelopes if state.spent > 0],
        key=lambda state: state.spent,
        reverse=True,
    )[:6]

    to_review = list(
        db.scalars(
            select(Transaction)
            .where(Transaction.envelope_id.is_(None), Transaction.kind != "transfer")
            .order_by(Transaction.op_date.desc())
            .limit(5)
        )
    )

    card_blocks = [
        {
            "card": card,
            "outstanding": cards.outstanding_cents(db, card),
            "next_due": cards.next_due(db, card),
        }
        for card in cards.deferred_cards(db)
    ]

    return render(
        request,
        "dashboard.html",
        active="dashboard",
        card_blocks=card_blocks,
        projected=cards.projected_balances(db, balances),
        unmatched_settlements=cards.unmatched_settlements(db),
        period=period,
        prev_period=shift_period(period, -1),
        next_period=shift_period(period, 1),
        summary=summary,
        accounts=accounts,
        balances=balances,
        total_balance=sum(balances.values()),
        series=series,
        spark=analytics.sparkline_points([point.net for point in series]),
        overspent=overspent,
        biggest=biggest,
        to_review=to_review,
        provisioned=provisioned,
        reference_total=calibration.reference_total(db),
        review_proposals=calibration.review_pending(db, period),
        rule_suggestions=categorizer.suggest_rules(db, limit=5),
        insurance_alerts=insurance.alerts(db),
        insurance_totals=insurance.totals(db),
        security=savings.security_fund(db),
        goals=savings.all_progress(db)[:4],
        today=date.today(),
    )
