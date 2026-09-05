"""Objectifs d'épargne, rythme de versement et fonds de sécurité."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Account,
    Envelope,
    SavingsContribution,
    SavingsGoal,
    Setting,
    Transaction,
)
from .budget import (
    current_period,
    month_income_expense,
    period_bounds,
    period_of,
    shift_period,
)
from .categorizer import normalize_label

SECURITY_MONTHS_KEY = "security_fund_months"
DEFAULT_SECURITY_MONTHS = 4

KIND_LABEL = {
    "security": "Épargne de sécurité",
    "project": "Projet",
    "other": "Autre",
}


@dataclass
class GoalProgress:
    goal: SavingsGoal
    saved_cents: int
    percent: float
    remaining_cents: int
    months_left: int | None
    required_monthly_cents: int
    pace_monthly_cents: int
    projected_date: date | None
    on_track: bool

    @property
    def label(self) -> str:
        return KIND_LABEL.get(self.goal.kind, "Objectif")


def saved_for(db: Session, goal: SavingsGoal) -> int:
    total = db.scalar(
        select(func.sum(SavingsContribution.amount_cents)).where(
            SavingsContribution.goal_id == goal.id
        )
    )
    return int(total or 0)


def recent_pace(db: Session, goal: SavingsGoal, months: int = 6) -> int:
    """Versement mensuel moyen constaté sur la période récente."""
    today = date.today()
    start, _ = period_bounds(shift_period(period_of(today), -(months - 1)))
    total = db.scalar(
        select(func.sum(SavingsContribution.amount_cents)).where(
            SavingsContribution.goal_id == goal.id,
            SavingsContribution.op_date >= start,
        )
    )
    return int(total or 0) // months


def months_until(target: date | None, today: date | None = None) -> int | None:
    if target is None:
        return None
    today = today or date.today()
    delta = (target.year - today.year) * 12 + (target.month - today.month)
    return max(delta, 0)


def progress_for(db: Session, goal: SavingsGoal, today: date | None = None) -> GoalProgress:
    today = today or date.today()
    saved = saved_for(db, goal)
    remaining = max(goal.target_cents - saved, 0)
    months_left = months_until(goal.target_date, today)
    pace = recent_pace(db, goal)

    if months_left and remaining:
        required = -(-remaining // months_left)
    elif remaining and goal.monthly_plan_cents:
        required = goal.monthly_plan_cents
    else:
        required = 0

    projected = None
    effective_pace = max(pace, goal.monthly_plan_cents)
    if remaining and effective_pace > 0:
        months_needed = -(-remaining // effective_pace)
        index = today.year * 12 + (today.month - 1) + months_needed
        projected = date(index // 12, index % 12 + 1, 1)

    on_track = remaining == 0 or (
        effective_pace >= required if required else effective_pace > 0
    )

    return GoalProgress(
        goal=goal,
        saved_cents=saved,
        percent=(saved / goal.target_cents) if goal.target_cents else 0.0,
        remaining_cents=remaining,
        months_left=months_left,
        required_monthly_cents=required,
        pace_monthly_cents=pace,
        projected_date=projected,
        on_track=on_track,
    )


def all_progress(db: Session, today: date | None = None) -> list[GoalProgress]:
    goals = db.scalars(
        select(SavingsGoal)
        .where(SavingsGoal.archived.is_(False))
        .order_by(SavingsGoal.priority, SavingsGoal.id)
    )
    return [progress_for(db, goal, today) for goal in goals]


def security_months(db: Session) -> int:
    setting = db.get(Setting, SECURITY_MONTHS_KEY)
    if setting and setting.value.isdigit():
        return int(setting.value)
    return DEFAULT_SECURITY_MONTHS


def set_security_months(db: Session, months: int) -> None:
    setting = db.get(Setting, SECURITY_MONTHS_KEY)
    if setting is None:
        setting = Setting(key=SECURITY_MONTHS_KEY, value=str(months))
        db.add(setting)
    else:
        setting.value = str(months)
    db.commit()


def essential_monthly_spending(db: Session, months: int = 6, today: date | None = None) -> int:
    """Dépense mensuelle « incompressible » moyenne.

    Base de calcul du fonds de sécurité : moyenne des dépenses affectées à des
    enveloppes marquées essentielles. Sans enveloppe marquée, on retombe sur
    la dépense totale moyenne — plus prudent que de renvoyer zéro.
    """
    today = today or date.today()
    period = period_of(today)
    history = [shift_period(period, -i) for i in range(1, months + 1)]
    if not history:
        return 0

    essential_ids = [
        env_id
        for env_id in db.scalars(select(Envelope.id).where(Envelope.essential.is_(True)))
    ]

    total = 0
    for step in history:
        start, end = period_bounds(step)
        if essential_ids:
            amount = db.scalar(
                select(func.sum(Transaction.amount_cents)).where(
                    Transaction.op_date >= start,
                    Transaction.op_date <= end,
                    Transaction.envelope_id.in_(essential_ids),
                    Transaction.kind != "transfer",
                )
            )
            total += -int(amount or 0)
        else:
            total += month_income_expense(db, step)[1]

    return max(total // len(history), 0)


@dataclass
class SecurityFund:
    months_target: int
    monthly_need_cents: int
    target_cents: int
    saved_cents: int
    percent: float
    months_covered: float
    goal: SavingsGoal | None


def security_fund(db: Session, today: date | None = None) -> SecurityFund:
    """Vue synthétique de l'épargne de précaution."""
    months = security_months(db)
    monthly_need = essential_monthly_spending(db, today=today)
    target = monthly_need * months

    goal = db.scalar(
        select(SavingsGoal)
        .where(SavingsGoal.kind == "security", SavingsGoal.archived.is_(False))
        .order_by(SavingsGoal.id)
    )
    saved = saved_for(db, goal) if goal else 0
    if goal is None:
        # À défaut d'objectif dédié, on lit le solde des comptes d'épargne.
        from .budget import account_balances

        balances = account_balances(db)
        savings_accounts = db.scalars(
            select(Account.id).where(Account.kind == "savings", Account.archived.is_(False))
        )
        saved = sum(balances.get(account_id, 0) for account_id in savings_accounts)

    if goal is not None and goal.target_cents:
        target = goal.target_cents

    return SecurityFund(
        months_target=months,
        monthly_need_cents=monthly_need,
        target_cents=target,
        saved_cents=saved,
        percent=(saved / target) if target else 0.0,
        months_covered=(saved / monthly_need) if monthly_need else 0.0,
        goal=goal,
    )


def add_contribution(
    db: Session, goal: SavingsGoal, amount_cents: int, when: date, note: str = ""
) -> SavingsContribution:
    contribution = SavingsContribution(
        goal_id=goal.id, amount_cents=amount_cents, op_date=when, note=note[:200]
    )
    db.add(contribution)
    db.commit()
    return contribution


# --------------------------------------------------------------------------
# Détection des comptes d'épargne à partir des relevés
# --------------------------------------------------------------------------

# Un versement vers une épargne non déclarée ressemble à une dépense : il
# gonfle le budget et il manque au fonds de sécurité. Ces libellés le
# trahissent — l'ordre compte, le plus précis d'abord.
SAVINGS_LABEL_PATTERNS: list[tuple[str, str]] = [
    ("LIVRET DEVELOPPEMENT", "LDDS"),
    ("LIVRET JEUNE", "Livret jeune"),
    ("LIVRET A", "Livret A"),
    ("ASSURANCE VIE", "Assurance vie"),
    ("PLAN EPARGNE LOGEMENT", "PEL"),
    ("COMPTE EPARGNE LOGEMENT", "CEL"),
    ("LDDS", "LDDS"),
    ("LEP", "Livret d'épargne populaire"),
    ("PEA", "PEA"),
    ("PEL", "PEL"),
    ("CEL", "CEL"),
    ("EPARGNE", "Compte d'épargne"),
]

SAVINGS_HINT_MIN_COUNT = 2


def _matches_savings(normalized: str, pattern: str) -> bool:
    """Motif cherché comme mot entier.

    Indispensable pour les sigles courts : « LEP » se cache dans
    « TELEPHONE », « PEL » dans « CHAPELLE ». Une simple sous-chaîne
    fabriquerait des comptes d'épargne à partir de factures de téléphone.
    """
    return re.search(rf"\b{re.escape(pattern)}\b", normalized) is not None


@dataclass(frozen=True)
class SavingsHint:
    """Une destination d'épargne repérée dans les libellés, non déclarée."""

    pattern: str
    suggested_name: str
    count: int
    total_cents: int
    months: int

    @property
    def monthly_cents(self) -> int:
        return self.total_cents // max(self.months, 1)


def _declared_savings_names(db: Session) -> list[str]:
    return [
        (name or "").upper()
        for name in db.scalars(
            select(Account.name).where(Account.archived.is_(False))
        )
    ]


def suggest_savings_accounts(db: Session, months: int = 12) -> list[SavingsHint]:
    """Versements réguliers vers une épargne qui n'est pas encore un compte.

    Tant que le compte n'existe pas, ces montants sont comptés comme des
    dépenses : le budget paraît plus lourd qu'il ne l'est, et l'épargne de
    sécurité paraît vide. C'est le premier écart à corriger.
    """
    horizon = shift_period(current_period(), -months)
    declared = _declared_savings_names(db)

    rows = db.execute(
        select(
            Transaction.raw_label,
            Transaction.label,
            Transaction.amount_cents,
            func.strftime("%Y-%m", Transaction.op_date),
        ).where(
            Transaction.amount_cents < 0,
            Transaction.kind != "transfer",
            func.strftime("%Y-%m", Transaction.op_date) >= horizon,
        )
    ).all()

    buckets: dict[str, dict] = {}
    for raw, label, amount, period in rows:
        normalized = normalize_label(raw or label)
        for pattern, name in SAVINGS_LABEL_PATTERNS:
            if not _matches_savings(normalized, pattern):
                continue
            # Déjà déclaré sous ce nom : plus rien à proposer.
            if any(pattern in declared_name for declared_name in declared):
                break
            bucket = buckets.setdefault(
                pattern, {"name": name, "count": 0, "total": 0, "periods": set()}
            )
            bucket["count"] += 1
            bucket["total"] += -amount
            bucket["periods"].add(period)
            break

    hints = [
        SavingsHint(
            pattern=pattern,
            suggested_name=bucket["name"],
            count=bucket["count"],
            total_cents=bucket["total"],
            months=len(bucket["periods"]),
        )
        for pattern, bucket in buckets.items()
        if bucket["count"] >= SAVINGS_HINT_MIN_COUNT
    ]
    hints.sort(key=lambda hint: hint.total_cents, reverse=True)
    return hints


def declare_savings_account(
    db: Session, pattern: str, name: str, opening_balance_cents: int = 0
) -> tuple[Account, int]:
    """Crée le compte d'épargne et requalifie les versements passés.

    Les opérations correspondantes deviennent des **virements internes** :
    elles sortent des dépenses du budget et cessent de fausser les moyennes.
    Retourne le compte et le nombre d'opérations requalifiées.
    """
    pattern = pattern.upper().strip()
    position = int(db.scalar(select(func.max(Account.position))) or 0) + 1
    account = Account(
        name=name.strip()[:120] or "Épargne",
        kind="savings",
        opening_balance_cents=opening_balance_cents,
        # Un compte d'épargne ne participe pas au « reste à budgéter » :
        # l'argent qui s'y trouve est déjà arbitré.
        is_budgeted=False,
        position=position,
    )
    db.add(account)
    db.flush()

    requalified = 0
    for transaction in db.scalars(
        select(Transaction).where(
            Transaction.amount_cents < 0, Transaction.kind != "transfer"
        )
    ):
        label = normalize_label(transaction.raw_label or transaction.label)
        if not _matches_savings(label, pattern):
            continue
        transaction.kind = "transfer"
        transaction.envelope_id = None
        requalified += 1

    db.commit()
    return account, requalified
