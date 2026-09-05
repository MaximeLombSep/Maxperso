"""Calibrage du budget sur l'historique, puis revue quand la réalité s'écarte.

Trois temps, qui suivent la façon dont on installe puis tient un budget :

  1. **Calibrage** — juste après un import, l'application lit ce qui a été
     réellement dépensé mois par mois et propose une dotation de référence
     par enveloppe. Rien n'est écrit sans validation explicite.
  2. **Application** — la référence est recopiée telle quelle dans chaque
     nouveau mois, puis ne bouge plus. Un budget qui se recalcule tout seul
     n'est plus un budget, c'est un relevé : il suit les dépenses au lieu de
     les cadrer.
  3. **Revue** — lorsque l'écart entre la référence et les dépenses réelles
     s'installe, l'application le signale et propose un ajustement chiffré,
     à accepter ou à refuser.

La médiane est préférée à la moyenne partout : une facture exceptionnelle ne
doit pas gonfler durablement une enveloppe.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Allocation, Envelope, Setting, Transaction
from .budget import current_period, months_between, period_label, shift_period

# Clés de la table `settings`.
AUTO_APPLY_KEY = "budget_auto_apply"
REVIEW_DISMISSED_KEY = "budget_review_dismissed"

# Seuils de la revue : un écart n'est signalé que s'il est à la fois
# significatif en valeur (sinon on harcèle pour trois euros) et en
# proportion (sinon une grosse enveloppe déclenche en permanence).
REVIEW_MIN_DELTA_CENTS = 1500  # 15 €
REVIEW_MIN_RATIO = 0.15
REVIEW_WINDOW = 3  # mois complets observés

# Calibrage : au-delà, l'historique le plus ancien n'apprend plus rien.
CALIBRATION_MONTHS = 12
CALIBRATION_MIN_CENTS = 500  # 5 € : en dessous, l'enveloppe n'a pas d'objet


# --------------------------------------------------------------------------
# Propositions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    """Un changement de dotation de référence proposé à l'utilisateur."""

    envelope: Envelope
    current_cents: int
    proposed_cents: int
    periods: list[str] = field(default_factory=list)
    observed: list[int] = field(default_factory=list)  # dépense de chaque mois
    reason: str = "calibrage"  # calibrage | hausse | baisse | dormante | nouvelle
    detail: str = ""

    @property
    def delta_cents(self) -> int:
        return self.proposed_cents - self.current_cents

    @property
    def months(self) -> int:
        return len(self.periods)

    @property
    def labels(self) -> list[str]:
        return [period_label(period) for period in self.periods]

    def recent(self, count: int = 6) -> list[tuple[str, int]]:
        """Derniers mois observés, du plus ancien au plus récent."""
        pairs = list(zip(self.labels, self.observed))
        return pairs[-count:]


def _median(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def _round_to_euro(cents: int) -> int:
    """Une dotation de référence en euros ronds : personne ne budgète 137,43 €."""
    return ((cents + 50) // 100) * 100


# --------------------------------------------------------------------------
# Lecture de l'historique
# --------------------------------------------------------------------------


def data_periods(db: Session) -> list[str]:
    """Mois où des opérations existent, du plus ancien au plus récent."""
    rows = db.execute(
        select(func.strftime("%Y-%m", Transaction.op_date))
        .where(Transaction.kind != "transfer")
        .group_by(func.strftime("%Y-%m", Transaction.op_date))
        .order_by(func.strftime("%Y-%m", Transaction.op_date))
    ).all()
    return [period for (period,) in rows if period]


def observed_periods(db: Session, months: int, before: str | None = None) -> list[str]:
    """Les `months` derniers mois **complets** exploitables.

    Le mois en cours est exclu : il est incomplet par construction, et sa
    dépense partielle ferait baisser artificiellement toute référence. Le
    mois le plus ancien est écarté dès qu'il en reste assez, parce qu'un
    relevé commence rarement un 1er : ce mois-là est presque toujours tronqué.
    """
    limit = before or current_period()
    complete = [period for period in data_periods(db) if period < limit]
    if len(complete) >= 4:
        complete = complete[1:]
    return complete[-months:]


def spend_timeline(db: Session) -> dict[tuple[int, str], int]:
    """Dépense (positive) par (enveloppe, mois), hors virements internes."""
    rows = db.execute(
        select(
            Transaction.envelope_id,
            func.strftime("%Y-%m", Transaction.op_date),
            func.sum(Transaction.amount_cents),
        )
        .where(
            Transaction.envelope_id.is_not(None),
            Transaction.kind != "transfer",
            Transaction.amount_cents < 0,
        )
        .group_by(Transaction.envelope_id, func.strftime("%Y-%m", Transaction.op_date))
    ).all()
    return {(env_id, period): -int(total or 0) for env_id, period, total in rows}


def _budgetable(db: Session) -> list[Envelope]:
    """Enveloppes qui se dotent : ni revenus, ni archivées.

    Les provisions (`sinking`) sont exclues elles aussi : leur dotation se
    déduit d'un objectif et d'une échéance, pas de ce qui a été dépensé —
    calibrer une provision vacances sur les vacances de l'an dernier n'aurait
    aucun sens.
    """
    return list(
        db.scalars(
            select(Envelope)
            .where(
                Envelope.archived.is_(False),
                Envelope.kind == "monthly",
            )
            .order_by(Envelope.position, Envelope.name)
        )
    )


# --------------------------------------------------------------------------
# 1. Calibrage initial
# --------------------------------------------------------------------------


def calibrate(db: Session, months: int = CALIBRATION_MONTHS) -> list[Proposal]:
    """Propose une dotation de référence par enveloppe, tirée de l'historique.

    N'écrit rien : la liste retournée est destinée à un écran de validation.
    """
    periods = observed_periods(db, months)
    if not periods:
        return []

    spend = spend_timeline(db)
    proposals: list[Proposal] = []

    for envelope in _budgetable(db):
        observed = [spend.get((envelope.id, period), 0) for period in periods]
        median = _median(observed)
        proposed = _round_to_euro(median) if median >= CALIBRATION_MIN_CENTS else 0

        if proposed == envelope.planned_cents:
            continue

        if proposed == 0:
            if envelope.planned_cents == 0:
                continue
            reason, detail = "dormante", "Aucune dépense sur la période observée."
        elif envelope.planned_cents == 0:
            reason, detail = "nouvelle", "Dépense régulière sans dotation de référence."
        else:
            reason = "hausse" if proposed > envelope.planned_cents else "baisse"
            detail = "Dotation actuelle éloignée de la dépense médiane."

        proposals.append(
            Proposal(
                envelope=envelope,
                current_cents=envelope.planned_cents,
                proposed_cents=proposed,
                periods=periods,
                observed=observed,
                reason=reason,
                detail=detail,
            )
        )

    proposals.sort(key=lambda p: p.proposed_cents, reverse=True)
    return proposals


def apply_proposals(db: Session, proposals: list[Proposal], keep_ids: set[int]) -> int:
    """Écrit les dotations de référence retenues. Retourne le nombre d'écritures."""
    written = 0
    for proposal in proposals:
        if proposal.envelope.id not in keep_ids:
            continue
        envelope = db.get(Envelope, proposal.envelope.id)
        if envelope is None or envelope.planned_cents == proposal.proposed_cents:
            continue
        envelope.planned_cents = proposal.proposed_cents
        written += 1
    if written:
        db.commit()
    return written


def reference_total(db: Session) -> int:
    """Somme des dotations de référence : zéro tant que rien n'est calibré."""
    return int(
        db.scalar(
            select(func.sum(Envelope.planned_cents)).where(
                Envelope.archived.is_(False), Envelope.kind != "income"
            )
        )
        or 0
    )


# --------------------------------------------------------------------------
# 2. Application de la référence à un mois
# --------------------------------------------------------------------------


def month_untouched(db: Session, period: str) -> bool:
    """Le mois n'a jamais été doté — pas même à zéro."""
    return not db.scalar(
        select(Allocation.id).where(Allocation.period == period).limit(1)
    )


def apply_reference(db: Session, period: str) -> int:
    """Recopie la dotation de référence dans le mois, sans écraser l'existant."""
    existing = {
        envelope_id
        for (envelope_id,) in db.execute(
            select(Allocation.envelope_id).where(Allocation.period == period)
        ).all()
    }
    written = 0
    for envelope in _budgetable(db):
        if envelope.id in existing or envelope.planned_cents <= 0:
            continue
        db.add(
            Allocation(
                envelope_id=envelope.id,
                period=period,
                allocated_cents=envelope.planned_cents,
            )
        )
        written += 1
    if written:
        db.commit()
    return written


def auto_apply_enabled(db: Session) -> bool:
    setting = db.get(Setting, AUTO_APPLY_KEY)
    return setting is None or setting.value != "0"


def set_auto_apply(db: Session, enabled: bool) -> None:
    setting = db.get(Setting, AUTO_APPLY_KEY)
    if setting is None:
        setting = Setting(key=AUTO_APPLY_KEY)
        db.add(setting)
    setting.value = "1" if enabled else "0"
    db.commit()


def ensure_provisioned(db: Session, period: str) -> int:
    """Dote le mois en cours depuis la référence, une seule fois.

    Appelée à l'affichage : c'est le seul endroit où l'application écrit sans
    qu'on le lui demande. Trois garde-fous rendent le geste prévisible — le
    mois doit être celui en cours, n'avoir jamais été touché, et une référence
    doit exister. Une fois la moindre dotation posée, plus rien n'est réécrit.
    """
    if period != current_period():
        return 0
    if not auto_apply_enabled(db):
        return 0
    if not month_untouched(db, period):
        return 0
    if reference_total(db) <= 0:
        return 0
    return apply_reference(db, period)


# --------------------------------------------------------------------------
# 3. Revue du budget
# --------------------------------------------------------------------------


def _significant(current: int, proposed: int) -> bool:
    delta = abs(proposed - current)
    if delta < REVIEW_MIN_DELTA_CENTS:
        return False
    if current > 0 and delta < int(current * REVIEW_MIN_RATIO):
        return False
    return True


def review(db: Session, period: str | None = None) -> list[Proposal]:
    """Écarts durables entre la référence et la dépense réelle.

    Ne renvoie rien tant qu'il n'y a pas assez de mois complets : un budget
    ne se révise pas sur une seule observation.
    """
    limit = period or current_period()
    # Réviser un budget qui n'existe pas n'a pas de sens : c'est un calibrage
    # qu'il faut proposer, et l'écran d'accueil s'en charge.
    if reference_total(db) <= 0:
        return []
    periods = observed_periods(db, REVIEW_WINDOW, before=limit)
    if len(periods) < REVIEW_WINDOW:
        return []

    spend = spend_timeline(db)
    proposals: list[Proposal] = []

    for envelope in _budgetable(db):
        observed = [spend.get((envelope.id, p), 0) for p in periods]
        median = _median(observed)
        proposed = _round_to_euro(median) if median >= CALIBRATION_MIN_CENTS else 0
        current = envelope.planned_cents

        if current == 0 and proposed == 0:
            continue
        if not _significant(current, proposed):
            continue

        if proposed == 0:
            reason = "dormante"
            detail = f"Rien de dépensé depuis {len(periods)} mois."
        elif current == 0:
            reason = "nouvelle"
            detail = "Dépense installée, sans dotation de référence."
        elif proposed > current:
            reason = "hausse"
            detail = f"Dépassée de façon régulière sur {len(periods)} mois."
        else:
            reason = "baisse"
            detail = f"Dotation nettement supérieure au réel sur {len(periods)} mois."

        proposals.append(
            Proposal(
                envelope=envelope,
                current_cents=current,
                proposed_cents=proposed,
                periods=periods,
                observed=observed,
                reason=reason,
                detail=detail,
            )
        )

    proposals.sort(key=lambda p: abs(p.delta_cents), reverse=True)
    return proposals


def review_dismissed_for(db: Session) -> str:
    setting = db.get(Setting, REVIEW_DISMISSED_KEY)
    return setting.value if setting is not None else ""


def dismiss_review(db: Session, period: str) -> None:
    """Range la proposition de revue jusqu'au mois suivant."""
    setting = db.get(Setting, REVIEW_DISMISSED_KEY)
    if setting is None:
        setting = Setting(key=REVIEW_DISMISSED_KEY)
        db.add(setting)
    setting.value = period
    db.commit()


def review_pending(db: Session, period: str | None = None) -> list[Proposal]:
    """Revue à proposer maintenant, ou liste vide si elle a été écartée ce mois-ci."""
    limit = period or current_period()
    dismissed = review_dismissed_for(db)
    if dismissed and months_between(dismissed, limit) <= 0:
        return []
    return review(db, limit)


def next_review_period(db: Session) -> str:
    """Mois où la revue écartée reviendra d'elle-même."""
    dismissed = review_dismissed_for(db)
    return shift_period(dismissed, 1) if dismissed else current_period()
