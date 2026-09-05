"""Cartes à débit différé : encours, cycles et rapprochement du prélèvement.

Le problème que résout ce module : une carte à débit différé dissocie la
**date de la dépense** de la **date de la sortie d'argent**. Un achat du
3 mars pèse sur le budget de mars, mais quitte le compte courant fin mars
(ou début avril) sous la forme d'un prélèvement unique.

Deux pièges en découlent, tous deux traités ici :

  1. **Double comptage** — si le prélèvement global était traité comme une
     dépense ordinaire, chaque euro serait compté deux fois : une fois à
     l'achat, une fois au règlement. Un prélèvement rapproché est donc
     requalifié en virement interne (`kind="transfer"`), donc neutre pour le
     budget, et les achats qu'il solde sont marqués réglés.
  2. **Solde trompeur** — tant que le prélèvement n'est pas passé, le compte
     courant affiche un solde qui ignore l'encours déjà engagé. Le solde
     projeté (`projected_balances`) retranche cet encours.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Account, Transaction
from .budget import period_of, shift_period

# Libellés de prélèvement de carte différée rencontrés chez les banques
# françaises. Comparés au libellé brut : la normalisation, elle, retire
# justement « FACTURE CARTE » du libellé.
SETTLEMENT_PATTERNS = re.compile(
    r"FACTURE\s+CARTE|DEBIT\s+DIFFERE|CARTE\s+DIFFEREE|ECHEANCE\s+CARTE|"
    r"RELEVE\s+CARTE|TOTAL\s+DES\s+FACTURES|REGLEMENT\s+CARTE|DEBIT\s+CARTE|"
    r"ACHATS\s+CARTE|CARTE\s+A\s+DEBIT\s+DIFFERE",
    re.IGNORECASE,
)

MATCH_WINDOW_DAYS = 8


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def looks_like_settlement(label: str) -> bool:
    return bool(SETTLEMENT_PATTERNS.search(_strip_accents(label).upper()))


# --------------------------------------------------------------------------
# Cycles de facturation
# --------------------------------------------------------------------------


def day_in_month(year: int, month: int, day: int) -> date:
    """Jour du mois, `0` valant « dernier jour » (et bornant février)."""
    last = calendar.monthrange(year, month)[1]
    if not day or day > last:
        return date(year, month, last)
    return date(year, month, day)


def cutoff_date(card: Account, period: str) -> date:
    year, month = int(period[:4]), int(period[5:])
    return day_in_month(year, month, card.cutoff_day)


def cycle_of(card: Account, purchase_date: date) -> str:
    """Cycle de facturation auquel un achat se rattache."""
    period = period_of(purchase_date)
    if purchase_date <= cutoff_date(card, period):
        return period
    return shift_period(period, 1)


def settlement_date(card: Account, period: str) -> date:
    """Date de prélèvement du cycle.

    Si le jour de prélèvement précède le jour d'arrêté, le prélèvement tombe
    le mois suivant : c'est le cas d'un arrêté en fin de mois réglé le 5.
    """
    year, month = int(period[:4]), int(period[5:])
    cutoff = day_in_month(year, month, card.cutoff_day).day
    settle = day_in_month(year, month, card.settlement_day).day
    if settle >= cutoff:
        return day_in_month(year, month, card.settlement_day)
    following = shift_period(period, 1)
    return day_in_month(int(following[:4]), int(following[5:]), card.settlement_day)


@dataclass
class Cycle:
    card: Account
    period: str
    settlement_on: date
    purchases: list[Transaction] = field(default_factory=list)
    settled_by: Transaction | None = None

    @property
    def total_cents(self) -> int:
        """Montant du cycle, négatif (les remboursements le réduisent)."""
        return sum(tx.amount_cents for tx in self.purchases)

    @property
    def amount_due_cents(self) -> int:
        return -self.total_cents

    @property
    def settled(self) -> bool:
        return self.settled_by is not None

    def is_closed(self, today: date | None = None) -> bool:
        """Un cycle est clos quand son arrêté est passé : plus aucun achat
        ne viendra s'y ajouter, le montant est donc figé."""
        today = today or date.today()
        return cutoff_date(self.card, self.period) < today


def deferred_cards(db: Session) -> list[Account]:
    return list(
        db.scalars(
            select(Account)
            .where(Account.kind == "credit", Account.archived.is_(False))
            .order_by(Account.position, Account.name)
        )
    )


def cycles(db: Session, card: Account, today: date | None = None) -> list[Cycle]:
    """Cycles de la carte, du plus récent au plus ancien."""
    today = today or date.today()
    rows = list(
        db.scalars(
            select(Transaction)
            .where(Transaction.account_id == card.id)
            .order_by(Transaction.op_date)
        )
    )

    grouped: dict[str, Cycle] = {}
    for tx in rows:
        period = cycle_of(card, tx.op_date)
        cycle = grouped.get(period)
        if cycle is None:
            cycle = Cycle(card=card, period=period, settlement_on=settlement_date(card, period))
            grouped[period] = cycle
        cycle.purchases.append(tx)
        if tx.settlement_id and cycle.settled_by is None:
            cycle.settled_by = db.get(Transaction, tx.settlement_id)

    current = cycle_of(card, today)
    if current not in grouped:
        grouped[current] = Cycle(
            card=card, period=current, settlement_on=settlement_date(card, current)
        )

    return sorted(grouped.values(), key=lambda c: c.period, reverse=True)


def outstanding_cents(db: Session, card: Account) -> int:
    """Encours non encore prélevé, en négatif."""
    total = db.scalar(
        select(func.sum(Transaction.amount_cents)).where(
            Transaction.account_id == card.id, Transaction.settlement_id.is_(None)
        )
    )
    return int(total or 0)


def outstanding_by_card(db: Session) -> dict[int, int]:
    return {card.id: outstanding_cents(db, card) for card in deferred_cards(db)}


def next_due(db: Session, card: Account, today: date | None = None) -> Cycle | None:
    """Prochain cycle à régler : le plus ancien encore non soldé."""
    today = today or date.today()
    pending = [
        cycle
        for cycle in cycles(db, card, today)
        if not cycle.settled and cycle.purchases
    ]
    return min(pending, key=lambda c: c.settlement_on) if pending else None


# --------------------------------------------------------------------------
# Rapprochement du prélèvement
# --------------------------------------------------------------------------


def candidate_settlements(
    db: Session, card: Account, cycle: Cycle, window_days: int = MATCH_WINDOW_DAYS
) -> list[Transaction]:
    """Débits du compte de règlement pouvant solder ce cycle.

    Fenêtre volontairement large et tri par pertinence : le montant exact
    d'abord, puis le libellé, puis la proximité de date. L'utilisateur
    tranche quand l'automatique n'a pas conclu.
    """
    if not card.settlement_account_id:
        return []

    start = cycle.settlement_on - timedelta(days=window_days)
    end = cycle.settlement_on + timedelta(days=window_days)
    rows = list(
        db.scalars(
            select(Transaction).where(
                Transaction.account_id == card.settlement_account_id,
                Transaction.amount_cents < 0,
                Transaction.op_date >= start,
                Transaction.op_date <= end,
                Transaction.settles_account_id.is_(None),
            )
        )
    )

    def rank(tx: Transaction) -> tuple[int, int, int]:
        exact = 0 if tx.amount_cents == cycle.total_cents else 1
        labelled = 0 if looks_like_settlement(tx.raw_label or tx.label) else 1
        return (exact, labelled, abs((tx.op_date - cycle.settlement_on).days))

    return sorted(rows, key=rank)


def link_settlement(
    db: Session, card: Account, cycle: Cycle, settlement: Transaction
) -> int:
    """Rattache un prélèvement à un cycle. Retourne le nombre d'achats soldés.

    Le prélèvement devient un virement interne : il ne pèse plus sur les
    dépenses du mois, qui portent déjà chaque achat à sa date.
    """
    settlement.kind = "transfer"
    settlement.envelope_id = None
    settlement.settles_account_id = card.id
    settlement.settles_period = cycle.period
    settlement.reviewed = True
    db.flush()

    soldes = 0
    for tx in cycle.purchases:
        if tx.settlement_id is None:
            tx.settlement_id = settlement.id
            soldes += 1

    cycle.settled_by = settlement
    db.commit()
    return soldes


def attach_settlement(
    db: Session,
    card: Account,
    settlement: Transaction,
    purchases: list[Transaction],
    period: str | None = None,
) -> int:
    """Rattache un règlement à une liste d'achats explicite.

    Utilisé quand le relevé nomme lui-même le lien — un PDF qui détaille les
    achats d'une carte puis annonce le montant prélevé. Aucun cycle n'est
    calculé : la banque a déjà tranché.
    """
    settlement.kind = "transfer"
    settlement.envelope_id = None
    settlement.settles_account_id = card.id
    settlement.settles_period = period or period_of(settlement.op_date)
    settlement.reviewed = True
    db.flush()

    attached = 0
    for purchase in purchases:
        if purchase.settlement_id is None:
            purchase.settlement_id = settlement.id
            attached += 1
    db.commit()
    return attached


def unlink_settlement(db: Session, settlement: Transaction) -> int:
    """Défait un rapprochement erroné et rend au prélèvement son statut."""
    released = 0
    for tx in db.scalars(
        select(Transaction).where(Transaction.settlement_id == settlement.id)
    ):
        tx.settlement_id = None
        released += 1

    settlement.kind = "expense" if settlement.amount_cents < 0 else "income"
    settlement.settles_account_id = None
    settlement.settles_period = None
    db.commit()
    return released


def auto_match(db: Session, today: date | None = None) -> int:
    """Rapproche les prélèvements dont le montant correspond exactement.

    Seule la correspondance exacte est automatisée : rattacher un montant
    approchant reviendrait à fausser silencieusement le budget. Le reste est
    proposé à l'écran, à valider d'un clic.
    """
    today = today or date.today()
    matched = 0

    for card in deferred_cards(db):
        if not card.settlement_account_id:
            continue
        for cycle in cycles(db, card, today):
            if cycle.settled or not cycle.purchases or not cycle.is_closed(today):
                continue
            for candidate in candidate_settlements(db, card, cycle):
                if candidate.amount_cents == cycle.total_cents:
                    link_settlement(db, card, cycle, candidate)
                    matched += 1
                    break

    return matched


def unmatched_settlements(db: Session, today: date | None = None) -> list[Transaction]:
    """Débits qui ressemblent à un règlement de carte sans être rapprochés.

    Tant qu'ils ne le sont pas, ils comptent comme une dépense ordinaire :
    le mois est doublement chargé. C'est l'alerte la plus utile de l'écran.
    """
    cards = deferred_cards(db)
    if not cards:
        return []

    settlement_accounts = {card.settlement_account_id for card in cards}
    settlement_accounts.discard(None)
    if not settlement_accounts:
        return []

    rows = db.scalars(
        select(Transaction)
        .where(
            Transaction.account_id.in_(settlement_accounts),
            Transaction.amount_cents < 0,
            Transaction.settles_account_id.is_(None),
            Transaction.kind != "transfer",
        )
        .order_by(Transaction.op_date.desc())
    )
    return [tx for tx in rows if looks_like_settlement(tx.raw_label or tx.label)]


def projected_balances(db: Session, balances: dict[int, int]) -> dict[int, int]:
    """Solde des comptes une fois les encours de carte prélevés."""
    projected = dict(balances)
    for card in deferred_cards(db):
        target = card.settlement_account_id
        if target in projected:
            projected[target] += outstanding_cents(db, card)
    return projected
