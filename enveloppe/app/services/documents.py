"""Pièces jointes des contrats : stockage disque contrôlé.

Le nom de fichier fourni par le navigateur n'est jamais utilisé pour
construire un chemin (traversée de répertoire) : on génère un nom aléatoire
et on conserve le nom d'origine en base, pour l'affichage seulement.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from ..config import settings

ALLOWED_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif",
    ".doc", ".docx", ".odt", ".txt", ".eml", ".csv", ".xlsx",
}
ALLOWED_MIME_PREFIXES = ("application/pdf", "image/", "text/", "application/vnd", "message/rfc822", "application/msword")

DOC_TYPES = {
    "contrat": "Contrat / Conditions",
    "avenant": "Avenant",
    "attestation": "Attestation",
    "echeancier": "Échéancier / Avis",
    "facture": "Quittance / Facture",
    "sinistre": "Sinistre",
    "autre": "Autre",
}

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ ()\-]")


class DocumentError(ValueError):
    pass


def safe_display_name(filename: str) -> str:
    name = _SAFE_NAME.sub("_", (filename or "document").strip())[:255]
    return name or "document"


def extension_of(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def contract_dir(contract_id: int) -> Path:
    path = settings.documents_dir / str(int(contract_id))
    path.mkdir(parents=True, exist_ok=True)
    return path


def store(contract_id: int, filename: str, content: bytes, mime: str) -> dict:
    """Valide et écrit le fichier. Retourne les métadonnées à enregistrer."""
    extension = extension_of(filename)
    if extension not in ALLOWED_EXTENSIONS:
        raise DocumentError(
            f"Extension « {extension or '?'} » non autorisée. "
            f"Formats acceptés : {', '.join(sorted(ALLOWED_EXTENSIONS))}."
        )
    if mime and not mime.startswith(ALLOWED_MIME_PREFIXES):
        raise DocumentError(f"Type de fichier non autorisé ({mime}).")

    limit = settings.max_upload_mb * 1024 * 1024
    if len(content) > limit:
        raise DocumentError(f"Fichier trop volumineux (limite {settings.max_upload_mb} Mo).")
    if not content:
        raise DocumentError("Fichier vide.")

    stored_name = f"{uuid.uuid4().hex}{extension}"
    destination = contract_dir(contract_id) / stored_name
    destination.write_bytes(content)
    destination.chmod(0o600)

    return {
        "original_name": safe_display_name(filename),
        "stored_name": stored_name,
        "mime": mime or "application/octet-stream",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def path_for(contract_id: int, stored_name: str) -> Path:
    """Chemin absolu vérifié : refuse tout nom sortant du dossier du contrat."""
    base = contract_dir(contract_id).resolve()
    candidate = (base / Path(stored_name).name).resolve()
    if not str(candidate).startswith(str(base)):
        raise DocumentError("Chemin de document invalide.")
    return candidate


def delete(contract_id: int, stored_name: str) -> None:
    try:
        path_for(contract_id, stored_name).unlink(missing_ok=True)
    except DocumentError:
        pass


def human_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} o"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} Ko"
    return f"{size_bytes / (1024 * 1024):.1f} Mo".replace(".", ",")
