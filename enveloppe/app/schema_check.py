"""Détection des écarts de schéma — sans jamais y toucher.

`create_all` sait créer les tables absentes, pas ajouter une colonne à une
table existante. Une base créée par une version antérieure doit donc être
migrée à la main, après sauvegarde et sur décision explicite. Ce module se
contente de constater l'écart et de dire quoi exécuter.
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, inspect

from .models import Base

logger = logging.getLogger("enveloppe.schema")

MIGRATIONS_HINT = "docs/migrations/"


def missing_columns(engine: Engine) -> dict[str, list[str]]:
    """Colonnes attendues par le modèle et absentes des tables existantes."""
    inspector = inspect(engine)
    gaps: dict[str, list[str]] = {}

    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue  # `create_all` la créera complète.
        present = {column["name"] for column in inspector.get_columns(table.name)}
        absent = sorted(column.name for column in table.columns if column.name not in present)
        if absent:
            gaps[table.name] = absent

    return gaps


def report(engine: Engine) -> dict[str, list[str]]:
    """Journalise l'écart éventuel. Ne modifie rien."""
    gaps = missing_columns(engine)
    if gaps:
        detail = "; ".join(f"{table} → {', '.join(cols)}" for table, cols in gaps.items())
        logger.warning(
            "Schéma de base incomplet (%s). Aucune modification automatique n'est "
            "appliquée : sauvegardez la base, puis exécutez le script de migration "
            "correspondant dans %s",
            detail,
            MIGRATIONS_HINT,
        )
    return gaps
