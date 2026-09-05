"""Rapprochement bancaire : confronter l'application au relevé.

C'est le geste qui sépare un budget crédible d'un budget approximatif. Tant
qu'on n'a pas confronté ses chiffres au relevé, on ne sait pas si l'écart
vient d'une opération oubliée, d'un import incomplet ou d'une erreur de
saisie — on sait seulement que les soldes ne se ressemblent pas.

Trois notions :

  - **pointée** : l'opération a été retrouvée sur le relevé de la banque ;
  - **solde pointé** : solde d'ouverture plus les seules opérations pointées.
    C'est ce que la banque devrait afficher ;
  - **écart** : la différence entre ce solde et celui du relevé. Zéro clôt le
    rapprochement ; sinon, l'ajustement est proposé, jamais imposé.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Account, Transaction

ADJUSTMENT_LABEL = "Ajustement de rapprochement"


@dataclass
class ReconcileState:
    """Où en est un compte vis-à-vis de son relevé."""

    account: Account
    cleared_cents: int = 0
    uncleared_cents: int = 0
    uncleared_count: int = 0
    pending: list[Transaction] = field(default_factory=list)

    @property
    def book_cents(self) -> int:
        """Solde de l'application : pointé plus non pointé."""
        return self.cleared_cents + self.uncleared_cents

    def gap(self, statement_cents: int) -> int:
        """Écart entre le relevé et le solde pointé."""
        return statement_cents - self.cleared_cents


def state_of(db: Session, account: Account, limit: int = 200) -> ReconcileState:
    """État courant du compte : ce qui est pointé, ce qui ne l'est pas."""
    cleared = int(
        db.scalar(
            select(func.sum(Transaction.amount_cents)).where(
                Transaction.account_id == account.id,
                Transaction.cleared.is_(True),
            )
        )
        or 0
    )
    uncleared_rows = list(
        db.scalars(
            select(Transaction)
            .where(
                Transaction.account_id == account.id,
                Transaction.cleared.is_(False),
            )
            .order_by(Transaction.op_date.desc(), Transaction.id.desc())
            .limit(limit)
        )
    )
    uncleared = int(
        db.scalar(
            select(func.sum(Transaction.amount_cents)).where(
                Transaction.account_id == account.id,
                Transaction.cleared.is_(False),
            )
        )
        or 0
    )
    count = int(
        db.scalar(
            select(func.count(Transaction.id)).where(
                Transaction.account_id == account.id,
                Transaction.cleared.is_(False),
            )
        )
        or 0
    )

    return ReconcileState(
        account=account,
        cleared_cents=account.opening_balance_cents + cleared,
        uncleared_cents=uncleared,
        uncleared_count=count,
        pending=uncleared_rows,
    )


def mark_cleared(db: Session, account: Account, ids: set[int]) -> int:
    """Pointe les opérations retrouvées sur le relevé. Retourne le nombre traité."""
    if not ids:
        return 0
    touched = 0
    for transaction in db.scalars(
        select(Transaction).where(
            Transaction.account_id == account.id,
            Transaction.id.in_(list(ids)),
            Transaction.cleared.is_(False),
        )
    ):
        transaction.cleared = True
        touched += 1
    if touched:
        db.commit()
    return touched


def unmark_cleared(db: Session, account: Account, ids: set[int]) -> int:
    """Dépointe — une opération pointée par erreur doit pouvoir être reprise."""
    if not ids:
        return 0
    touched = 0
    for transaction in db.scalars(
        select(Transaction).where(
            Transaction.account_id == account.id,
            Transaction.id.in_(list(ids)),
            Transaction.cleared.is_(True),
        )
    ):
        transaction.cleared = False
        touched += 1
    if touched:
        db.commit()
    return touched


def create_adjustment(
    db: Session, account: Account, cents: int, when: date | None = None
) -> Transaction:
    """Écrit l'écart résiduel comme une opération, plutôt que de le masquer.

    Un rapprochement qui « tombe juste » parce qu'on a bricolé un solde
    d'ouverture ne vaut rien : l'écart doit rester lisible dans l'historique,
    avec sa date, pour qu'on puisse y revenir.
    """
    when = when or date.today()
    transaction = Transaction(
        account_id=account.id,
        op_date=when,
        amount_cents=cents,
        label=ADJUSTMENT_LABEL,
        raw_label=ADJUSTMENT_LABEL,
        normalized_label=ADJUSTMENT_LABEL.upper(),
        kind="income" if cents > 0 else "expense",
        cleared=True,
        reviewed=True,
        fingerprint=f"ajustement-{account.id}-{when}-{cents}",
    )
    db.add(transaction)
    db.commit()
    return transaction


def close(
    db: Session, account: Account, statement_cents: int, when: date | None = None
) -> None:
    """Clôt le rapprochement : le solde du relevé devient le repère."""
    account.reconciled_on = when or date.today()
    account.reconciled_balance_cents = statement_cents
    db.commit()
