"""Analyses : séries mensuelles, répartition, récurrences, pistes d'économies."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Account, Envelope, Transaction
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


# --------------------------------------------------------------------------
# Âge de l'argent
# --------------------------------------------------------------------------

# Comme la méthode d'origine : la moyenne porte sur les dernières sorties,
# pas sur tout l'historique. Un mois exceptionnel ne doit pas figer
# l'indicateur pendant un an.
AGE_SAMPLE = 10
AGE_MIN_SAMPLE = 3
AGE_MIN_COVERAGE = 0.5


@dataclass(frozen=True)
class MoneyAge:
    """Depuis combien de jours dormait l'argent que vous venez de dépenser."""

    days: int | None
    sampled: int = 0
    coverage: float = 0.0

    @property
    def known(self) -> bool:
        return self.days is not None

    @property
    def comment(self) -> str:
        if self.days is None:
            return "Pas encore assez d'historique pour le calculer."
        if self.days >= 30:
            return "Vous vivez sur l'argent du mois précédent."
        if self.days >= 15:
            return "Vous sortez du fil du rasoir."
        return "Vous dépensez l'argent presque dès qu'il arrive."


def money_age(db: Session, sample: int = AGE_SAMPLE) -> MoneyAge:
    """Quatrième règle : faire vieillir son argent, et le mesurer.

    Les entrées sont consommées par les sorties dans l'ordre d'arrivée — le
    premier euro entré est le premier dépensé. L'âge retenu est la moyenne
    des délais, pondérée par les montants, sur les dernières dépenses.

    Renvoie `days=None` plutôt qu'un chiffre trompeur quand l'historique ne
    remonte pas assez loin : une dépense payée avec de l'argent entré avant
    le début des relevés n'est appariable à rien.
    """
    rows = db.execute(
        select(Transaction.op_date, Transaction.amount_cents)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.kind != "transfer",
            Account.is_budgeted.is_(True),
            Account.archived.is_(False),
        )
        .order_by(Transaction.op_date, Transaction.id)
    ).all()

    queue: deque[list] = deque()  # [date d'entrée, centimes restants]
    outflows: list[tuple[int, int]] = []  # (somme pondérée des âges, montant apparié)

    for when, amount in rows:
        if amount > 0:
            queue.append([when, amount])
            continue

        needed = -amount
        weighted = 0
        matched = 0
        while needed > 0 and queue:
            entry = queue[0]
            taken = min(entry[1], needed)
            weighted += taken * (when - entry[0]).days
            matched += taken
            needed -= taken
            entry[1] -= taken
            if entry[1] == 0:
                queue.popleft()
        outflows.append((weighted, matched))

    recent = [item for item in outflows[-sample:] if item[1] > 0]
    if len(recent) < AGE_MIN_SAMPLE:
        return MoneyAge(days=None, sampled=len(recent))

    requested = len(outflows[-sample:])
    coverage = len(recent) / requested if requested else 0.0
    if coverage < AGE_MIN_COVERAGE:
        return MoneyAge(days=None, sampled=len(recent), coverage=coverage)

    total_weighted = sum(weighted for weighted, _ in recent)
    total_matched = sum(matched for _, matched in recent)
    return MoneyAge(
        days=max(total_weighted // total_matched, 0),
        sampled=len(recent),
        coverage=coverage,
    )


# --------------------------------------------------------------------------
# Solde mois par mois
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BalancePoint:
    period: str
    cents: int

    @property
    def label(self) -> str:
        from .budget import period_label

        return period_label(self.period)

    @property
    def short(self) -> str:
        from .budget import period_short

        return period_short(self.period)


def balance_series(
    db: Session, months: int = 12, end: str | None = None
) -> list[BalancePoint]:
    """Solde à la fin de chaque mois, comptes courants et épargne.

    Les cartes à débit différé sont exclues : leur « solde » est un encours à
    payer, pas de l'argent disponible. L'additionner à un compte courant
    reviendrait à compter deux fois la même dépense.

    Le point de départ tient compte de tout ce qui précède la fenêtre : sans
    cela, la courbe partirait du solde d'ouverture des comptes, c'est-à-dire
    d'un chiffre que personne n'a jamais vu.
    """
    from .budget import current_period, shift_period

    last = end or current_period()
    first = shift_period(last, -(months - 1))

    accounts = list(
        db.scalars(
            select(Account).where(
                Account.archived.is_(False), Account.kind != "credit"
            )
        )
    )
    if not accounts:
        return []

    account_ids = [account.id for account in accounts]
    opening = sum(account.opening_balance_cents for account in accounts)

    rows = db.execute(
        select(
            func.strftime("%Y-%m", Transaction.op_date),
            func.sum(Transaction.amount_cents),
        )
        .where(Transaction.account_id.in_(account_ids))
        .group_by(func.strftime("%Y-%m", Transaction.op_date))
    ).all()
    by_period = {period: int(total or 0) for period, total in rows if period}

    if not by_period:
        return []

    # Inutile de tracer six mois de trait plat avant le premier relevé : la
    # courbe commence là où l'historique commence.
    first = max(first, min(by_period))
    if first >= last:
        return []

    # Tout ce qui précède la fenêtre est replié dans le point de départ.
    running = opening + sum(
        cents for period, cents in by_period.items() if period < first
    )

    points: list[BalancePoint] = []
    step = first
    while step <= last:
        running += by_period.get(step, 0)
        points.append(BalancePoint(period=step, cents=running))
        step = shift_period(step, 1)
    return points


# Repère du tracé, en unités du `viewBox`. Le dessin est calculé ici plutôt
# que dans le gabarit : une géométrie se teste, une expression Jinja non.
CHART_W = 360
CHART_H = 170
CHART_LEFT = 38
CHART_RIGHT = 352
CHART_TOP = 14
CHART_BOTTOM = 146


@dataclass(frozen=True)
class ChartPoint:
    x: float
    y: float
    point: BalancePoint


@dataclass(frozen=True)
class BalanceChart:
    """Tracé prêt à écrire dans un `<svg>`, bornes comprises."""

    marks: list[ChartPoint] = field(default_factory=list)
    line: str = ""
    area: str = ""
    grid: list[tuple[float, int]] = field(default_factory=list)
    zero_y: float | None = None
    low_cents: int = 0
    high_cents: int = 0

    @property
    def last(self) -> ChartPoint | None:
        return self.marks[-1] if self.marks else None

    @property
    def lowest(self) -> ChartPoint | None:
        return min(self.marks, key=lambda m: m.point.cents) if self.marks else None


def balance_chart(points: list[BalancePoint]) -> BalanceChart:
    """Géométrie de la courbe de solde.

    L'échelle n'est pas ancrée à zéro : sur un solde de 8 000 € qui varie de
    300 €, un axe partant de zéro écraserait la courbe en trait plat. La
    borne basse est donc affichée en clair sur la grille, et le zéro reçoit
    sa propre ligne dès que le solde passe dans le rouge.
    """
    if len(points) < 2:
        return BalanceChart()

    values = [point.cents for point in points]
    low, high = min(values), max(values)
    if low < 0:
        high = max(high, 0)
    span = max(high - low, 1)
    margin = max(span // 12, 100)
    low -= margin
    high += margin
    span = high - low

    def to_y(cents: int) -> float:
        ratio = (cents - low) / span
        return round(CHART_BOTTOM - ratio * (CHART_BOTTOM - CHART_TOP), 2)

    step = (CHART_RIGHT - CHART_LEFT) / (len(points) - 1)
    marks = [
        ChartPoint(x=round(CHART_LEFT + index * step, 2), y=to_y(point.cents), point=point)
        for index, point in enumerate(points)
    ]

    line = "M" + " L".join(f"{mark.x} {mark.y}" for mark in marks)
    area = (
        f"{line} L{marks[-1].x} {CHART_BOTTOM} L{marks[0].x} {CHART_BOTTOM} Z"
    )

    grid = [(to_y(value), value) for value in (high, (high + low) // 2, low)]

    return BalanceChart(
        marks=marks,
        line=line,
        area=area,
        grid=grid,
        zero_y=to_y(0) if low < 0 < high else None,
        low_cents=low,
        high_cents=high,
    )
