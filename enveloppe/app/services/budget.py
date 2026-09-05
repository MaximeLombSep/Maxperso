"""Moteur budgétaire : enveloppes, dotations, report, reste à budgéter."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Account,
    Allocation,
    Envelope,
    EnvelopeGroup,
    Setting,
    Transaction,
)

# Types d'enveloppes qui se dotent. « monthly » ajoute la dotation prévue
# chaque mois ; « refill » complète jusqu'à ce montant, ce qui reste dedans
# venant en déduction — c'est la différence entre « je mets 400 € de plus »
# et « je remets l'enveloppe à 400 € ».
FUNDED_KINDS = ("monthly", "refill")
ENVELOPE_KINDS = ("monthly", "refill", "sinking", "income")

ABSORB_OVERSPEND_KEY = "absorb_overspend"

MONTHS_FR = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


# --------------------------------------------------------------------------
# Périodes
# --------------------------------------------------------------------------


def period_of(day: date) -> str:
    return day.strftime("%Y-%m")


def current_period() -> str:
    return period_of(date.today())


def parse_period(period: str) -> tuple[int, int]:
    year, month = period.split("-")
    return int(year), int(month)


def shift_period(period: str, months: int) -> str:
    year, month = parse_period(period)
    index = year * 12 + (month - 1) + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def period_label(period: str) -> str:
    year, month = parse_period(period)
    return f"{MONTHS_FR[month - 1]} {year}"


def period_bounds(period: str) -> tuple[date, date]:
    year, month = parse_period(period)
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def months_between(start: str, end: str) -> int:
    y1, m1 = parse_period(start)
    y2, m2 = parse_period(end)
    return (y2 * 12 + m2) - (y1 * 12 + m1)


def period_range(start: str, end: str) -> list[str]:
    count = months_between(start, end)
    if count < 0:
        return []
    return [shift_period(start, i) for i in range(count + 1)]


# --------------------------------------------------------------------------
# Agrégats
# --------------------------------------------------------------------------


@dataclass
class EnvelopeState:
    envelope: Envelope
    carry_in: int = 0
    allocated: int = 0
    activity: int = 0  # négatif pour une dépense
    available: int = 0
    planned: int = 0
    suggested: int = 0

    @property
    def spent(self) -> int:
        """Dépense du mois, en positif."""
        return -self.activity if self.activity < 0 else 0

    @property
    def budget_total(self) -> int:
        return self.carry_in + self.allocated

    @property
    def progress(self) -> float:
        base = self.budget_total
        if base <= 0:
            return 1.0 if self.spent > 0 else 0.0
        return min(self.spent / base, 1.5)

    @property
    def overspent(self) -> bool:
        return self.available < 0

    @property
    def missing(self) -> int:
        """Ce qu'il reste à doter ce mois-ci pour tenir l'objectif.

        Une enveloppe mensuelle veut sa dotation prévue ; une enveloppe à
        recompléter veut revenir à son montant ; une provision veut sa part
        du mois. Zéro dès que l'objectif est atteint.
        """
        if self.envelope.kind == "income":
            return 0
        if self.envelope.kind == "monthly":
            return max(self.planned - self.allocated, 0)
        return max(self.suggested, 0)

    @property
    def funding(self) -> str:
        """État lisible d'un coup d'œil : rouge, orange, vert, ou rien.

        C'est le signal qui pilote la routine du mois : ce qui est dans le
        rouge se règle tout de suite, ce qui est orange attend d'être doté,
        le vert se laisse tranquille.
        """
        if self.envelope.kind == "income":
            return "income"
        if self.available < 0:
            return "overspent"
        if self.missing > 0:
            return "underfunded"
        if self.planned or self.allocated or self.available:
            return "funded"
        return "none"


@dataclass
class GroupState:
    group: EnvelopeGroup | None
    envelopes: list[EnvelopeState]

    @property
    def allocated(self) -> int:
        return sum(e.allocated for e in self.envelopes)

    @property
    def spent(self) -> int:
        return sum(e.spent for e in self.envelopes)

    @property
    def available(self) -> int:
        return sum(e.available for e in self.envelopes)


@dataclass
class MonthSummary:
    period: str
    groups: list[GroupState]
    income: int = 0
    expense: int = 0
    allocated: int = 0
    to_budget: int = 0
    available_total: int = 0
    uncategorized: int = 0
    absorbed: int = 0  # dépassements des mois passés, repris sur ce mois-ci

    @property
    def missing_total(self) -> int:
        return sum(state.missing for state in self.envelopes)

    @property
    def overspent_count(self) -> int:
        return sum(1 for state in self.envelopes if state.funding == "overspent")

    @property
    def label(self) -> str:
        return period_label(self.period)

    @property
    def net(self) -> int:
        return self.income - self.expense

    @property
    def savings_rate(self) -> float:
        return (self.income - self.expense) / self.income if self.income > 0 else 0.0

    @property
    def envelopes(self) -> list[EnvelopeState]:
        return [state for group in self.groups for state in group.envelopes]


def budget_start_period(db: Session) -> str | None:
    """Premier mois réellement budgété, c'est-à-dire doté.

    Repère central du moteur : importer douze mois d'historique ne crée
    aucune dette rétroactive. Tant qu'aucune dotation n'existe, il n'y a pas
    de budget à reporter — seulement des opérations à consulter.
    """
    return db.scalar(select(func.min(Allocation.period)))


def _activity_timeline(db: Session) -> dict[tuple[int, str], int]:
    """Somme des opérations par (enveloppe, période), hors virements internes."""
    rows = db.execute(
        select(
            Transaction.envelope_id,
            func.strftime("%Y-%m", Transaction.op_date),
            func.sum(Transaction.amount_cents),
        )
        .where(Transaction.envelope_id.is_not(None), Transaction.kind != "transfer")
        .group_by(Transaction.envelope_id, func.strftime("%Y-%m", Transaction.op_date))
    ).all()
    return {(env_id, period): int(total or 0) for env_id, period, total in rows}


def _allocation_timeline(db: Session) -> dict[tuple[int, str], int]:
    rows = db.execute(
        select(Allocation.envelope_id, Allocation.period, Allocation.allocated_cents)
    ).all()
    return {(env_id, period): int(cents or 0) for env_id, period, cents in rows}


def month_income_expense(db: Session, period: str) -> tuple[int, int]:
    """(revenus, dépenses) du mois sur les comptes budgétés, hors virements."""
    start, end = period_bounds(period)
    rows = db.execute(
        select(Transaction.amount_cents)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.op_date >= start,
            Transaction.op_date <= end,
            Transaction.kind != "transfer",
            Account.is_budgeted.is_(True),
        )
    ).all()
    income = sum(amount for (amount,) in rows if amount > 0)
    expense = -sum(amount for (amount,) in rows if amount < 0)
    return income, expense


def absorb_overspend(db: Session) -> bool:
    """Un dépassement est-il repris sur le reste à budgéter du mois suivant ?

    Troisième règle de la méthode : on encaisse le coup. Une enveloppe qui
    finit dans le rouge ne traîne pas son découvert de mois en mois — le
    dépassement est couvert par l'argent du mois suivant, et l'enveloppe
    repart de zéro. Désactivable pour qui préfère voir la dette rester où
    elle a été creusée.
    """
    setting = db.get(Setting, ABSORB_OVERSPEND_KEY)
    return setting is None or setting.value != "0"


def set_absorb_overspend(db: Session, enabled: bool) -> None:
    setting = db.get(Setting, ABSORB_OVERSPEND_KEY)
    if setting is None:
        setting = Setting(key=ABSORB_OVERSPEND_KEY)
        db.add(setting)
    setting.value = "1" if enabled else "0"
    db.commit()


def suggested_allocation(envelope: Envelope, period: str, available_now: int) -> int:
    """Dotation conseillée du mois.

    - enveloppe mensuelle : la dotation prévue ;
    - enveloppe à recompléter : ce qui manque pour revenir au montant prévu ;
    - provision (`sinking`) : ce qu'il reste à mettre de côté, étalé sur les
      mois restants jusqu'à l'échéance.
    """
    if envelope.kind == "refill":
        return max(envelope.planned_cents - max(available_now, 0), 0)
    if envelope.kind != "sinking" or not envelope.target_cents:
        return max(envelope.planned_cents, 0)

    missing = max(envelope.target_cents - available_now, 0)
    if not envelope.target_date:
        return envelope.planned_cents or missing

    target_period = period_of(envelope.target_date)
    remaining = months_between(period, target_period) + 1
    if remaining <= 0:
        return missing
    return -(-missing // remaining)  # arrondi au centime supérieur


def month_summary(db: Session, period: str) -> MonthSummary:
    """État complet d'un mois : report, dotations, dépenses, reste à budgéter."""
    envelopes = list(
        db.scalars(
            select(Envelope)
            .where(Envelope.archived.is_(False))
            .order_by(Envelope.position, Envelope.name)
        )
    )
    groups = {g.id: g for g in db.scalars(select(EnvelopeGroup).order_by(EnvelopeGroup.position))}

    activity = _activity_timeline(db)
    allocations = _allocation_timeline(db)
    budget_start = budget_start_period(db) or period
    global_start = min(budget_start, period)

    first_allocation: dict[int, str] = {}
    for (envelope_id, step) in allocations:
        current = first_allocation.get(envelope_id)
        if current is None or step < current:
            first_allocation[envelope_id] = step

    absorb = absorb_overspend(db)
    absorbed_total = 0

    states: dict[int, EnvelopeState] = {}
    for envelope in envelopes:
        if envelope.kind == "income":
            # Une enveloppe de revenus est une **catégorie**, pas une réserve :
            # l'argent encaissé alimente le reste à budgéter. Lui prêter en
            # plus un « disponible » afficherait la même somme deux fois.
            state = EnvelopeState(
                envelope=envelope,
                activity=activity.get((envelope.id, period), 0),
            )
            states[envelope.id] = state
            continue

        # Le report d'une enveloppe démarre au premier mois où elle a été
        # dotée : les mois antérieurs à sa mise en service ne comptent pas.
        env_start = first_allocation.get(envelope.id)
        env_start = max(global_start, env_start) if env_start else period
        timeline = period_range(min(env_start, period), period)

        carry = 0
        allocated = 0
        month_activity = 0
        for step in timeline:
            allocated = allocations.get((envelope.id, step), 0)
            month_activity = activity.get((envelope.id, step), 0)
            balance = carry + allocated + month_activity
            if step == period:
                break
            if balance < 0 and absorb:
                # Le découvert d'une enveloppe ne se promène pas de mois en
                # mois : il est couvert par l'argent du mois suivant, et
                # l'enveloppe repart à zéro.
                absorbed_total += -balance
                carry = 0
            else:
                carry = balance if envelope.rollover else 0
        state = EnvelopeState(
            envelope=envelope,
            carry_in=carry,
            allocated=allocated,
            activity=month_activity,
            available=carry + allocated + month_activity,
            planned=envelope.planned_cents,
        )
        # Pour une enveloppe à recompléter, l'objectif se mesure sur ce qui a
        # été mis dedans, pas sur ce qu'il en reste : dépenser en cours de
        # mois ne doit pas redemander une dotation déjà faite.
        base = (
            state.carry_in + state.allocated
            if envelope.kind == "refill"
            else state.available
        )
        state.suggested = suggested_allocation(envelope, period, base)
        states[envelope.id] = state

    grouped: list[GroupState] = []
    seen_groups: dict[int | None, GroupState] = {}
    for envelope in envelopes:
        key = envelope.group_id
        if key not in seen_groups:
            seen_groups[key] = GroupState(group=groups.get(key), envelopes=[])
            grouped.append(seen_groups[key])
        seen_groups[key].envelopes.append(states[envelope.id])

    income, expense = month_income_expense(db, period)

    # Reste à budgéter cumulé : tout ce qui est entré depuis le premier mois
    # budgété, moins tout ce qui a été affecté à une enveloppe. L'historique
    # importé avant la mise en place du budget n'entre pas dans le calcul.
    history = period_range(global_start, period)
    cumulative_income = sum(month_income_expense(db, step)[0] for step in history)
    cumulative_allocated = sum(
        cents for (_, step), cents in allocations.items() if step in set(history)
    )

    start, end = period_bounds(period)
    uncategorized = db.scalar(
        select(func.count(Transaction.id)).where(
            Transaction.op_date >= start,
            Transaction.op_date <= end,
            Transaction.envelope_id.is_(None),
            Transaction.kind != "transfer",
        )
    )

    return MonthSummary(
        period=period,
        groups=grouped,
        income=income,
        expense=expense,
        allocated=sum(
            allocations.get((envelope.id, period), 0)
            for envelope in envelopes
            if envelope.kind != "income"
        ),
        to_budget=cumulative_income - cumulative_allocated - absorbed_total,
        available_total=sum(s.available for s in states.values()),
        uncategorized=int(uncategorized or 0),
        absorbed=absorbed_total,
    )


def set_allocation(db: Session, envelope_id: int, period: str, cents: int) -> Allocation:
    allocation = db.scalar(
        select(Allocation).where(
            Allocation.envelope_id == envelope_id, Allocation.period == period
        )
    )
    if allocation is None:
        allocation = Allocation(envelope_id=envelope_id, period=period, allocated_cents=cents)
        db.add(allocation)
    else:
        allocation.allocated_cents = cents
    db.commit()
    return allocation


def _average_spending(db: Session, envelope_id: int, period: str, months: int = 3) -> int:
    """Dépense mensuelle moyenne d'une enveloppe sur les mois précédents."""
    activity = _activity_timeline(db)
    history = [shift_period(period, -index) for index in range(1, months + 1)]
    spends = [-activity.get((envelope_id, step), 0) for step in history]
    spends = [value for value in spends if value > 0]
    return sum(spends) // len(spends) if spends else 0


def move_between(
    db: Session, period: str, source_id: int, target_id: int, cents: int
) -> tuple[Envelope, Envelope]:
    """Déplace une dotation d'une enveloppe vers une autre, sur un mois.

    C'est le geste central du budget en enveloppe : quand l'une déborde, on
    ne fabrique pas d'argent, on en reprend ailleurs. La somme des dotations
    du mois reste donc inchangée, et le reste à budgéter avec elle.
    """
    if source_id == target_id:
        raise ValueError("Choisissez deux enveloppes différentes.")
    if cents <= 0:
        raise ValueError("Le montant à déplacer doit être positif.")

    source = db.get(Envelope, source_id)
    target = db.get(Envelope, target_id)
    if source is None or target is None:
        raise ValueError("Enveloppe introuvable.")
    if "income" in {source.kind, target.kind}:
        raise ValueError(
            "Une enveloppe de revenus ne se dote pas : elle ne peut ni donner "
            "ni recevoir."
        )

    current = {
        allocation.envelope_id: allocation.allocated_cents
        for allocation in db.scalars(
            select(Allocation).where(
                Allocation.envelope_id.in_([source_id, target_id]),
                Allocation.period == period,
            )
        )
    }
    set_allocation(db, source_id, period, current.get(source_id, 0) - cents)
    set_allocation(db, target_id, period, current.get(target_id, 0) + cents)
    return source, target


def autofill_month(db: Session, period: str, mode: str = "suggested") -> int:
    """Remplit les dotations vides du mois. Retourne le nombre d'enveloppes servies.

    `mode` :
      - `suggested` : dotation prévue, ou provision calculée pour les échéances ;
      - `last_month` : recopie du mois précédent ;
      - `average3` : moyenne des dépenses des trois derniers mois.
    """
    summary = month_summary(db, period)
    previous = shift_period(period, -1)
    filled = 0

    for state in summary.envelopes:
        if state.allocated:
            continue
        if state.envelope.kind == "income":
            continue

        if mode == "last_month":
            amount = db.scalar(
                select(Allocation.allocated_cents).where(
                    Allocation.envelope_id == state.envelope.id,
                    Allocation.period == previous,
                )
            ) or 0
        elif mode == "average3":
            amount = _average_spending(db, state.envelope.id, period) or state.suggested
        else:
            # Sans dotation prévue, la moyenne des dépenses récentes est le
            # point de départ le plus utile : le bouton sert dès le premier mois.
            amount = state.suggested or _average_spending(db, state.envelope.id, period)

        if amount:
            set_allocation(db, state.envelope.id, period, int(amount))
            filled += 1

    return filled


def account_balances(db: Session) -> dict[int, int]:
    """Solde par compte : solde d'ouverture + somme des opérations.

    Cas particulier des **cartes à débit différé** : leur « solde » est
    l'encours restant à prélever. Les achats déjà réglés en sont retirés —
    leur montant a quitté le compte courant, les compter encore reviendrait
    à les soustraire deux fois du patrimoine.
    """
    accounts = list(db.scalars(select(Account).where(Account.archived.is_(False))))
    balances = {account.id: account.opening_balance_cents for account in accounts}
    card_ids = {account.id for account in accounts if account.kind == "credit"}

    query = select(Transaction.account_id, func.sum(Transaction.amount_cents))
    if card_ids:
        query = query.where(Transaction.account_id.not_in(card_ids))
    for account_id, total in db.execute(query.group_by(Transaction.account_id)).all():
        if account_id in balances:
            balances[account_id] += int(total or 0)

    if card_ids:
        pending = db.execute(
            select(Transaction.account_id, func.sum(Transaction.amount_cents))
            .where(
                Transaction.account_id.in_(card_ids),
                Transaction.settlement_id.is_(None),
            )
            .group_by(Transaction.account_id)
        ).all()
        for account_id, total in pending:
            if account_id in balances:
                balances[account_id] += int(total or 0)

    return balances


def fund_targets(db: Session, period: str) -> tuple[int, int]:
    """Dote chaque enveloppe de ce qui lui manque pour tenir son objectif.

    Le geste central du mois : plutôt que de remplir les cases une à une, on
    demande à l'application de couvrir tous les objectifs, puis on arbitre
    s'il ne reste pas assez. Retourne (enveloppes servies, total affecté).
    """
    summary = month_summary(db, period)
    served = 0
    total = 0
    for state in summary.envelopes:
        missing = state.missing
        if missing <= 0:
            continue
        set_allocation(db, state.envelope.id, period, state.allocated + missing)
        served += 1
        total += missing
    return served, total
