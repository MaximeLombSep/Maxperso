"""Amorçage : plan d'enveloppes et règles de départ.

Idempotent — relancer ne duplique rien. Aucune donnée personnelle n'est
créée : uniquement une structure de travail modifiable.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Account, Envelope, EnvelopeGroup, Rule
from .categorizer import CATEGORY_KEYWORDS, SEED_RULES

GROUPS = [
    ("Revenus", 0),
    ("Logement", 1),
    ("Vie courante", 2),
    ("Transports", 3),
    ("Santé & Assurances", 4),
    ("Loisirs & Achats", 5),
    ("Provisions & Projets", 6),
]

# (nom, groupe, type, essentiel, couleur, icône)
ENVELOPES = [
    ("Salaire", "Revenus", "income", False, "#16a34a", "€"),
    ("Prestations", "Revenus", "income", False, "#22c55e", "€"),
    ("Loyer / Crédit", "Logement", "monthly", True, "#0ea5e9", "⌂"),
    ("Énergie", "Logement", "monthly", True, "#38bdf8", "⚡"),
    ("Eau", "Logement", "monthly", True, "#7dd3fc", "≈"),
    ("Téléphone / Internet", "Logement", "monthly", True, "#60a5fa", "☏"),
    ("Charges / Copropriété", "Logement", "monthly", True, "#93c5fd", "▤"),
    ("Courses", "Vie courante", "monthly", True, "#f59e0b", "▣"),
    ("Restaurants", "Vie courante", "monthly", False, "#fb923c", "◇"),
    ("Achats divers", "Vie courante", "monthly", False, "#fdba74", "◈"),
    ("Carburant", "Transports", "monthly", True, "#ef4444", "▲"),
    ("Transports", "Transports", "monthly", True, "#f87171", "→"),
    ("Entretien véhicule", "Transports", "sinking", False, "#fca5a5", "⚙"),
    ("Santé", "Santé & Assurances", "monthly", True, "#a855f7", "✚"),
    ("Assurances", "Santé & Assurances", "sinking", True, "#c084fc", "▣"),
    ("Mutuelle", "Santé & Assurances", "monthly", True, "#d8b4fe", "✚"),
    ("Loisirs", "Loisirs & Achats", "monthly", False, "#ec4899", "♪"),
    ("Abonnements", "Loisirs & Achats", "monthly", False, "#f472b6", "▶"),
    ("Vacances", "Provisions & Projets", "sinking", False, "#14b8a6", "☼"),
    ("Impôts", "Provisions & Projets", "sinking", True, "#64748b", "§"),
    ("Cadeaux / Fêtes", "Provisions & Projets", "sinking", False, "#f43f5e", "★"),
    ("Imprévus", "Provisions & Projets", "sinking", True, "#94a3b8", "!"),
]


def seed_rules(db: Session, envelopes: dict[str, Envelope] | None = None) -> int:
    """Pose les règles livrées qui manquent. Retourne le nombre ajouté.

    Deux familles, dans cet ordre de priorité :

      - les **enseignes** (« CARREFOUR », « EDF ») cherchées telles quelles ;
      - les **métiers** (« BOULANGERIE », « BOWLING ») cherchés comme mots
        entiers, ce qui rattrape les commerçants locaux qu'aucune liste
        d'enseignes ne contiendra. La recherche par mot entier est
        indispensable ici : « BAR » ne doit pas reconnaître « BARBIER ».

    Idempotent : relancer n'ajoute que ce qui manque, et ne touche jamais
    une règle existante — l'utilisateur a pu la corriger.
    """
    if envelopes is None:
        envelopes = {envelope.name: envelope for envelope in db.scalars(select(Envelope))}

    known = {rule.pattern for rule in db.scalars(select(Rule))}
    added = 0

    for pattern, envelope_name in SEED_RULES:
        if pattern in known or envelope_name not in envelopes:
            continue
        db.add(
            Rule(
                envelope_id=envelopes[envelope_name].id,
                matcher="contains",
                pattern=pattern,
                sign="credit" if envelopes[envelope_name].kind == "income" else "debit",
                priority=500,
            )
        )
        known.add(pattern)
        added += 1

    for keyword, envelope_name in CATEGORY_KEYWORDS:
        pattern = rf"\b{keyword}\b"
        if pattern in known or envelope_name not in envelopes:
            continue
        db.add(
            Rule(
                envelope_id=envelopes[envelope_name].id,
                matcher="regex",
                pattern=pattern,
                sign="debit",
                priority=800,  # après les enseignes : une marque est plus sûre
            )
        )
        known.add(pattern)
        added += 1

    if added:
        db.commit()
    return added


def bootstrap(db: Session, with_default_account: bool = True) -> None:
    groups: dict[str, EnvelopeGroup] = {}
    for name, position in GROUPS:
        existing = db.scalar(select(EnvelopeGroup).where(EnvelopeGroup.name == name))
        if existing is None:
            existing = EnvelopeGroup(name=name, position=position)
            db.add(existing)
            db.flush()
        groups[name] = existing

    envelopes: dict[str, Envelope] = {
        envelope.name: envelope for envelope in db.scalars(select(Envelope))
    }
    for position, (name, group_name, kind, essential, color, icon) in enumerate(ENVELOPES):
        if name in envelopes:
            continue
        envelope = Envelope(
            name=name,
            group_id=groups[group_name].id,
            kind=kind,
            essential=essential,
            color=color,
            icon=icon,
            position=position,
            rollover=kind != "income",
        )
        db.add(envelope)
        db.flush()
        envelopes[name] = envelope

    seed_rules(db, envelopes)

    if with_default_account and db.scalar(select(Account).limit(1)) is None:
        db.add(Account(name="Compte courant", kind="checking", position=0))

    db.commit()
