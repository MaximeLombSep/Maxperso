"""Mise en route : conduire l'installation d'un budget, une étape à la fois.

Toutes les pièces existaient — détection des comptes d'épargne, suggestions
de règles, calibrage, rapprochement — mais éparpillées sur six écrans, sans
ordre annoncé. Or l'ordre compte : calibrer avant d'avoir sorti les
virements d'épargne des dépenses fige des montants faux.

Ce module ne fait donc aucun calcul propre. Il lit l'état de chaque étape à
partir des données, ce qui rend le parcours reprenable : quitter au milieu
et revenir trois jours plus tard retrouve exactement le même point.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Account, Transaction
from . import calibration, categorizer
from . import savings as savings_service

SETUP_DISMISSED_KEY = "setup_dismissed"


@dataclass(frozen=True)
class Step:
    """Une étape du parcours, et où elle en est."""

    slug: str
    title: str
    intro: str
    done: bool
    pending: int = 0  # ce qui reste à traiter, quand ça se compte

    @property
    def status(self) -> str:
        return "done" if self.done else "todo"


ORDER = ["comptes", "revenus", "depenses", "budget", "suivi", "pret"]

TITLES = {
    "comptes": "Vos comptes",
    "revenus": "Ce qui entre",
    "depenses": "Ce qui sort",
    "budget": "Votre budget",
    "suivi": "Le rapprochement",
    "pret": "C'est prêt",
}


def has_transactions(db: Session) -> bool:
    return bool(db.scalar(select(Transaction.id).limit(1)))


def steps(db: Session) -> list[Step]:
    """État des étapes, déduit des données et non d'un drapeau stocké."""
    hints = savings_service.suggest_savings_accounts(db)
    revenus = categorizer.suggest_rules(db, sign="credit")
    depenses = categorizer.suggest_rules(db)
    reference = calibration.reference_total(db)
    rapproche = db.scalar(
        select(func.count(Account.id)).where(Account.reconciled_on.is_not(None))
    )

    imported = has_transactions(db)

    return [
        Step(
            slug="comptes",
            title=TITLES["comptes"],
            intro=(
                "Un virement vers un livret ou un compte de dépôt ressemble à une "
                "dépense tant que le compte n'existe pas. Le déclarer le sort du "
                "budget et corrige toutes les moyennes qui suivront."
            ),
            done=imported and not hints,
            pending=len(hints),
        ),
        Step(
            slug="revenus",
            title=TITLES["revenus"],
            intro=(
                "Salaire, prestations, remboursements réguliers : ce qui entre "
                "alimente le reste à budgéter. Une entrée non reconnue manque au "
                "budget du mois."
            ),
            done=imported and not revenus,
            pending=len(revenus),
        ),
        Step(
            slug="depenses",
            title=TITLES["depenses"],
            intro=(
                "Les commerçants et prélèvements que les règles livrées n'ont pas "
                "reconnus. Chaque règle acceptée classe aussi tout l'historique "
                "correspondant."
            ),
            done=imported and not depenses,
            pending=len(depenses),
        ),
        Step(
            slug="budget",
            title=TITLES["budget"],
            intro=(
                "Les montants sont déduits de ce que vous avez réellement dépensé, "
                "puis figés. À faire après les deux étapes précédentes : calibrer "
                "sur des dépenses mal classées fige des montants faux."
            ),
            done=reference > 0,
        ),
        Step(
            slug="suivi",
            title=TITLES["suivi"],
            intro=(
                "Confronter l'application à votre relevé, une fois, pour savoir "
                "que les chiffres sont justes. Ensuite, une fois par mois suffit."
            ),
            done=bool(rapproche),
        ),
        Step(
            slug="pret",
            title=TITLES["pret"],
            intro="Ce qui reste à faire, et ce qui tourne désormais tout seul.",
            done=False,
        ),
    ]


def progress(db: Session) -> tuple[int, int]:
    """(étapes faites, étapes à faire) — « pret » ne compte pas."""
    done = [step for step in steps(db) if step.slug != "pret"]
    return sum(1 for step in done if step.done), len(done)


def next_slug(slug: str) -> str | None:
    index = ORDER.index(slug)
    return ORDER[index + 1] if index + 1 < len(ORDER) else None


def previous_slug(slug: str) -> str | None:
    index = ORDER.index(slug)
    return ORDER[index - 1] if index > 0 else None


def first_unfinished(db: Session) -> str:
    """Là où reprendre : la première étape non terminée, sinon le récapitulatif."""
    for step in steps(db):
        if step.slug != "pret" and not step.done:
            return step.slug
    return "pret"


def dismissed(db: Session) -> bool:
    from ..models import Setting

    setting = db.get(Setting, SETUP_DISMISSED_KEY)
    return setting is not None and setting.value == "1"


def dismiss(db: Session, hidden: bool = True) -> None:
    """Range l'invitation du tableau de bord, sans effacer le parcours."""
    from ..models import Setting

    setting = db.get(Setting, SETUP_DISMISSED_KEY)
    if setting is None:
        setting = Setting(key=SETUP_DISMISSED_KEY)
        db.add(setting)
    setting.value = "1" if hidden else "0"
    db.commit()
