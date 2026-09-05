"""Contrats d'assurance : échéance anniversaire, préavis, coût annuel.

Repères de droit français utilisés pour les alertes (informatif, non
juridique) :
  - **Loi Chatel** : l'assureur doit rappeler la date limite de résiliation
    au plus tard 15 jours avant ; le préavis usuel est de deux mois.
  - **Loi Hamon** : résiliation possible à tout moment après un an de
    contrat pour l'auto, l'habitation et les contrats affinitaires.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import InsuranceContract

FREQUENCY_PER_YEAR = {"monthly": 12, "quarterly": 4, "semiannual": 2, "annual": 1}
FREQUENCY_LABEL = {
    "monthly": "mensuelle",
    "quarterly": "trimestrielle",
    "semiannual": "semestrielle",
    "annual": "annuelle",
}
CATEGORY_LABEL = {
    "auto": "Auto / Moto",
    "habitation": "Habitation",
    "sante": "Santé / Mutuelle",
    "prevoyance": "Prévoyance",
    "emprunteur": "Emprunteur",
    "scolaire": "Scolaire",
    "animal": "Animal",
    "mobile": "Mobile / Nomade",
    "juridique": "Protection juridique",
    "autre": "Autre",
}
HAMON_CATEGORIES = {"auto", "habitation", "mobile", "animal"}


def _safe_replace(day: date, year: int) -> date:
    """Anniversaire d'une date, 29 février ramené au 28."""
    try:
        return day.replace(year=year)
    except ValueError:
        return day.replace(year=year, day=28)


def next_anniversary(reference: date | None, today: date | None = None) -> date | None:
    """Prochaine occurrence annuelle de `reference` à partir d'aujourd'hui."""
    if reference is None:
        return None
    today = today or date.today()
    candidate = _safe_replace(reference, today.year)
    if candidate < today:
        candidate = _safe_replace(reference, today.year + 1)
    return candidate


def annual_cost_cents(contract: InsuranceContract) -> int:
    return contract.premium_cents * FREQUENCY_PER_YEAR.get(contract.frequency, 1)


def monthly_provision_cents(contract: InsuranceContract) -> int:
    """Ce qu'il faut mettre de côté chaque mois pour absorber la prime."""
    return -(-annual_cost_cents(contract) // 12)


@dataclass
class ContractStatus:
    contract: InsuranceContract
    renewal: date | None
    days_to_renewal: int | None
    notice_deadline: date | None
    days_to_deadline: int | None
    hamon_open_from: date | None
    hamon_available: bool
    level: str  # ok | info | warning | critical | expired
    message: str

    @property
    def annual_cost(self) -> int:
        return annual_cost_cents(self.contract)

    @property
    def monthly_provision(self) -> int:
        return monthly_provision_cents(self.contract)


def status_for(contract: InsuranceContract, today: date | None = None) -> ContractStatus:
    """Position du contrat dans son cycle annuel, et alerte associée."""
    today = today or date.today()
    reference = contract.renewal_date or contract.start_date
    renewal = next_anniversary(reference, today)

    days_to_renewal = (renewal - today).days if renewal else None
    deadline = (
        renewal - timedelta(days=max(contract.notice_period_days, 0)) if renewal else None
    )
    days_to_deadline = (deadline - today).days if deadline else None

    hamon_open_from = None
    if contract.start_date and (
        contract.hamon_eligible or contract.category in HAMON_CATEGORIES
    ):
        hamon_open_from = _safe_replace(contract.start_date, contract.start_date.year + 1)
    hamon_available = bool(hamon_open_from and hamon_open_from <= today)

    level, message = "ok", "Rien à faire pour l'instant."
    if contract.status == "ended":
        level, message = "expired", "Contrat clos."
    elif contract.status == "pending_cancel":
        level, message = "info", "Résiliation en cours."
    elif days_to_deadline is not None and 0 <= days_to_deadline <= 15:
        level = "critical"
        message = (
            f"Dernier moment pour résilier : {days_to_deadline} jour(s) "
            f"avant la date limite du {deadline:%d/%m/%Y}."
        )
    elif days_to_deadline is not None and 0 <= days_to_deadline <= 45:
        level = "warning"
        message = f"Fenêtre de résiliation ouverte jusqu'au {deadline:%d/%m/%Y}."
    elif days_to_deadline is not None and days_to_deadline < 0 and hamon_available:
        level = "info"
        message = "Préavis dépassé, mais résiliable à tout moment (loi Hamon)."
    elif days_to_renewal is not None and days_to_renewal <= 90:
        level = "info"
        message = f"Échéance dans {days_to_renewal} jour(s), prime à provisionner."

    return ContractStatus(
        contract=contract,
        renewal=renewal,
        days_to_renewal=days_to_renewal,
        notice_deadline=deadline,
        days_to_deadline=days_to_deadline,
        hamon_open_from=hamon_open_from,
        hamon_available=hamon_available,
        level=level,
        message=message,
    )


LEVEL_ORDER = {"critical": 0, "warning": 1, "info": 2, "ok": 3, "expired": 4}


def all_statuses(db: Session, today: date | None = None) -> list[ContractStatus]:
    contracts = db.scalars(
        select(InsuranceContract).order_by(InsuranceContract.name)
    )
    statuses = [status_for(contract, today) for contract in contracts]
    statuses.sort(
        key=lambda s: (LEVEL_ORDER[s.level], s.days_to_renewal if s.days_to_renewal is not None else 9999)
    )
    return statuses


def alerts(db: Session, today: date | None = None) -> list[ContractStatus]:
    return [s for s in all_statuses(db, today) if s.level in {"critical", "warning"}]


def totals(db: Session) -> dict[str, int]:
    contracts = list(
        db.scalars(select(InsuranceContract).where(InsuranceContract.status == "active"))
    )
    annual = sum(annual_cost_cents(c) for c in contracts)
    return {
        "count": len(contracts),
        "annual_cents": annual,
        "monthly_cents": -(-annual // 12) if annual else 0,
    }
