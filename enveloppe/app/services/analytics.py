"""Analyses : séries mensuelles, répartition, récurrences, pistes d'économies."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Envelope, Transaction
from .budget import (
    month_income_expense,
    period_bounds,
    period_of,
    period_range,
    shift_period,
)
from .categorizer import merchant_tokens

FEE_PATTERNS = re.compile(
    r"COTISATION|FRAIS\b|COMMISSION|AGIOS|INTERVENTION|IRREGULARITE|"
    r"OPPOSITION|TENUE DE COMPTE|ASSURANCE MOYENS",
    re.IGNORECASE,
)


@dataclass
class MonthPoint:
    period: str
    income: int
    expense: int

    @property
    def net(self) -> int:
        return self.income - self.expense

    @property
    def rate(self) -> float:
        return self.net / self.income if self.income > 0 else 0.0


def monthly_series(db: Session, months: int = 12, end: str | None = None) -> list[MonthPoint]:
    end = end or period_of(date.today())
    start = shift_period(end, -(months - 1))
    points = []
    for step in period_range(start, end):
        income, expense = month_income_expense(db, step)
        points.append(MonthPoint(period=step, income=income, expense=expense))
    return points


@dataclass
class BreakdownItem:
    envelope: Envelope | None
    spent_cents: int
    share: float


def breakdown(db: Session, period: str, limit: int = 12) -> list[BreakdownItem]:
    """Répartition des dépenses du mois par enveloppe, la plus lourde d'abord."""
    start, end = period_bounds(period)
    rows = db.execute(
        select(Transaction.envelope_id, func.sum(Transaction.amount_cents))
        .where(
            Transaction.op_date >= start,
            Transaction.op_date <= end,
            Transaction.amount_cents < 0,
            Transaction.kind != "transfer",
        )
        .group_by(Transaction.envelope_id)
    ).all()

    totals = [(env_id, -int(amount or 0)) for env_id, amount in rows]
    total_spent = sum(amount for _, amount in totals) or 1
    totals.sort(key=lambda item: item[1], reverse=True)

    items = []
    for env_id, amount in totals[:limit]:
        envelope = db.get(Envelope, env_id) if env_id else None
        items.append(
            BreakdownItem(envelope=envelope, spent_cents=amount, share=amount / total_spent)
        )
    return items


def top_merchants(db: Session, period: str, limit: int = 8) -> list[tuple[str, int, int]]:
    """(commerçant, total dépensé, nombre d'opérations) sur le mois."""
    start, end = period_bounds(period)
    rows = db.execute(
        select(Transaction.raw_label, Transaction.amount_cents).where(
            Transaction.op_date >= start,
            Transaction.op_date <= end,
            Transaction.amount_cents < 0,
            Transaction.kind != "transfer",
        )
    ).all()

    grouped: dict[str, list[int]] = defaultdict(list)
    for label, amount in rows:
        key = " ".join(merchant_tokens(label, limit=2)) or "Divers"
        grouped[key].append(-int(amount))

    ranked = sorted(
        ((name, sum(values), len(values)) for name, values in grouped.items()),
        key=lambda item: item[1],
        reverse=True,
    )
    return ranked[:limit]


@dataclass
class Recurring:
    merchant: str
    average_cents: int
    occurrences: int
    last_seen: date
    months: int
    essential: bool = False

    @property
    def annual_cents(self) -> int:
        return self.average_cents * 12


def recurring_charges(db: Session, months: int = 6, tolerance: float = 0.15) -> list[Recurring]:
    """Prélèvements réguliers : même commerçant, montant stable, plusieurs mois.

    C'est le premier gisement d'économies d'un budget : abonnements oubliés,
    services doublonnés, options bancaires jamais utilisées.
    """
    today = date.today()
    start, _ = period_bounds(shift_period(period_of(today), -(months - 1)))
    rows = db.execute(
        select(
            Transaction.raw_label,
            Transaction.amount_cents,
            Transaction.op_date,
            Transaction.envelope_id,
        ).where(
            Transaction.op_date >= start,
            Transaction.amount_cents < 0,
            Transaction.kind != "transfer",
        )
    ).all()

    essential_ids = set(
        db.scalars(select(Envelope.id).where(Envelope.essential.is_(True)))
    )

    grouped: dict[str, list[tuple[int, date, int | None]]] = defaultdict(list)
    for label, amount, op_date, envelope_id in rows:
        key = " ".join(merchant_tokens(label, limit=2))
        if key:
            grouped[key].append((-int(amount), op_date, envelope_id))

    found: list[Recurring] = []
    for merchant, entries in grouped.items():
        distinct_months = {entry[1].strftime("%Y-%m") for entry in entries}
        if len(distinct_months) < 3:
            continue
        amounts = [amount for amount, _, _ in entries]
        average = sum(amounts) // len(amounts)
        if average <= 0:
            continue
        spread = max(abs(amount - average) for amount in amounts) / average
        if spread > tolerance:
            continue
        essential_hits = sum(
            1 for _, _, envelope_id in entries if envelope_id in essential_ids
        )
        found.append(
            Recurring(
                merchant=merchant,
                average_cents=average,
                occurrences=len(entries),
                last_seen=max(entry[1] for entry in entries),
                months=len(distinct_months),
                essential=essential_hits * 2 >= len(entries),
            )
        )

    found.sort(key=lambda item: item.average_cents, reverse=True)
    return found


@dataclass
class Opportunity:
    title: str
    detail: str
    monthly_cents: int
    yearly_cents: int
    severity: str = "info"  # info | warning


@dataclass
class SavingsReport:
    opportunities: list[Opportunity] = field(default_factory=list)

    @property
    def monthly_total(self) -> int:
        return sum(item.monthly_cents for item in self.opportunities)

    @property
    def yearly_total(self) -> int:
        return sum(item.yearly_cents for item in self.opportunities)


def savings_opportunities(db: Session, period: str | None = None) -> SavingsReport:
    """Pistes chiffrées pour dégager de la marge, du plus concret au plus flou."""
    period = period or period_of(date.today())
    report = SavingsReport()

    # Un loyer ou une facture d'énergie sont récurrents mais ne constituent
    # pas une piste d'économie : seules les charges non essentielles comptent.
    subscriptions = [
        item
        for item in recurring_charges(db)
        if item.average_cents >= 300 and not item.essential
    ]
    if subscriptions:
        total = sum(item.average_cents for item in subscriptions)
        names = ", ".join(item.merchant.title() for item in subscriptions[:5])
        report.opportunities.append(
            Opportunity(
                title=f"{len(subscriptions)} prélèvements récurrents identifiés",
                detail=f"{names}… — à passer en revue : tout ce qui n'est plus utilisé est une économie immédiate.",
                monthly_cents=total,
                yearly_cents=total * 12,
            )
        )

    start, end = period_bounds(shift_period(period, -11))
    _, current_end = period_bounds(period)
    fee_rows = db.execute(
        select(Transaction.raw_label, Transaction.amount_cents).where(
            Transaction.op_date >= start,
            Transaction.op_date <= current_end,
            Transaction.amount_cents < 0,
        )
    ).all()
    fees = -sum(amount for label, amount in fee_rows if FEE_PATTERNS.search(label or ""))
    if fees > 0:
        report.opportunities.append(
            Opportunity(
                title="Frais bancaires sur 12 mois",
                detail="Cotisation carte, frais de tenue de compte, commissions d'intervention : "
                "négociables ou évitables en changeant d'offre.",
                monthly_cents=fees // 12,
                yearly_cents=fees,
                severity="warning",
            )
        )

    # Enveloppes en dérive : mois courant nettement au-dessus de la moyenne.
    history = [shift_period(period, -i) for i in (1, 2, 3)]
    for envelope in db.scalars(select(Envelope).where(Envelope.archived.is_(False))):
        if envelope.kind == "income":
            continue
        spends = []
        for step in history:
            step_start, step_end = period_bounds(step)
            amount = db.scalar(
                select(func.sum(Transaction.amount_cents)).where(
                    Transaction.envelope_id == envelope.id,
                    Transaction.op_date >= step_start,
                    Transaction.op_date <= step_end,
                    Transaction.amount_cents < 0,
                )
            )
            spends.append(-int(amount or 0))
        spends = [value for value in spends if value > 0]
        if len(spends) < 2:
            continue
        average = sum(spends) // len(spends)
        this_start, this_end = period_bounds(period)
        current = -int(
            db.scalar(
                select(func.sum(Transaction.amount_cents)).where(
                    Transaction.envelope_id == envelope.id,
                    Transaction.op_date >= this_start,
                    Transaction.op_date <= this_end,
                    Transaction.amount_cents < 0,
                )
            )
            or 0
        )
        excess = current - average
        if average > 0 and excess > max(average * 0.2, 2000):
            report.opportunities.append(
                Opportunity(
                    title=f"« {envelope.name} » au-dessus de sa moyenne",
                    detail=f"{current / 100:.0f} € ce mois contre {average / 100:.0f} € en moyenne "
                    "sur les trois mois précédents.",
                    monthly_cents=excess,
                    yearly_cents=excess * 12,
                    severity="warning",
                )
            )

    report.opportunities.sort(key=lambda item: item.yearly_cents, reverse=True)
    return report


def sparkline_points(values: list[int], width: int = 120, height: int = 32) -> str:
    """Polyline SVG normalisée — les graphiques sont rendus côté serveur."""
    if not values:
        return ""
    low, high = min(values), max(values)
    span = (high - low) or 1
    step = width / max(len(values) - 1, 1)
    coords = [
        f"{index * step:.1f},{height - ((value - low) / span) * height:.1f}"
        for index, value in enumerate(values)
    ]
    return " ".join(coords)
