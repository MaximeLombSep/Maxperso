"""Détection des écarts de schéma — sans jamais y toucher.

`create_all` sait créer les tables absentes, pas ajouter une colonne à une
table existante. Une base créée par une version antérieure doit donc être
migrée à la main, après sauvegarde et sur décision explicite. Ce module se
contente de constater l'écart et de dire quoi exécuter.
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, inspect
from sqlalchemy.dialects import sqlite

from .models import Base

logger = logging.getLogger("enveloppe.schema")

MIGRATIONS_HINT = "docs/migrations/"
SQLITE = sqlite.dialect()


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


def _ddl_for(column) -> str | None:
    """Instruction d'ajout d'une colonne, ou None si elle n'est pas sûre.

    Seul `ADD COLUMN` est produit : c'est la seule opération de schéma qui
    ne touche à aucune donnée existante. Une colonne obligatoire sans valeur
    par défaut est refusée — SQLite ne saurait pas quoi mettre dans les
    lignes déjà là, et deviner à sa place serait pire que s'arrêter.
    """
    kind = column.type.compile(dialect=SQLITE)
    clause = f'ADD COLUMN "{column.name}" {kind}'

    default = None
    if column.server_default is not None:
        default = str(column.server_default.arg)
    elif column.default is not None and not column.default.is_callable:
        value = column.default.arg
        if isinstance(value, bool):
            default = "1" if value else "0"
        elif isinstance(value, (int, float)):
            default = str(value)
        elif isinstance(value, str):
            escaped = value.replace("'", "''")
            default = f"'{escaped}'"

    if not column.nullable:
        if default is None:
            return None
        return f"{clause} NOT NULL DEFAULT {default}"
    if default is not None:
        return f"{clause} DEFAULT {default}"
    return clause


def apply_missing_columns(engine: Engine) -> tuple[list[str], list[str]]:
    """Ajoute les colonnes manquantes. Retourne (appliquées, refusées).

    N'est jamais appelée d'elle-même : il faut avoir activé l'option
    correspondante dans la configuration de l'add-on, ce qui vaut décision
    explicite. Rien d'autre qu'un ajout de colonne n'est exécuté ici — pas
    de suppression, pas de renommage, pas de modification de type.
    """
    gaps = missing_columns(engine)
    if not gaps:
        return [], []

    applied: list[str] = []
    refused: list[str] = []

    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            for name in gaps.get(table.name, []):
                column = table.columns[name]
                clause = _ddl_for(column)
                if clause is None:
                    refused.append(f"{table.name}.{name}")
                    logger.error(
                        "Colonne %s.%s non ajoutée : obligatoire et sans valeur "
                        "par défaut. Exécutez la migration correspondante à la main.",
                        table.name,
                        name,
                    )
                    continue
                statement = f'ALTER TABLE "{table.name}" {clause}'
                logger.warning("Migration : %s", statement)
                connection.exec_driver_sql(statement)
                applied.append(f"{table.name}.{name}")

    if applied:
        logger.warning(
            "%d colonne(s) ajoutée(s) : %s. Repassez l'option "
            "« apply_migrations » sur off, elle n'a plus lieu d'être.",
            len(applied),
            ", ".join(applied),
        )
    return applied, refused
