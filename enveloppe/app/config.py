"""Configuration de l'application.

Toutes les valeurs sensibles proviennent de l'environnement ou d'un fichier
généré à l'exécution (`secret.key`, permissions 0600). Aucun secret n'est
écrit en dur dans le code ni versionné.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "oui"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    secret_key: str
    currency: str
    session_max_age: int
    max_upload_mb: int
    behind_proxy: bool

    @property
    def db_path(self) -> Path:
        return self.data_dir / "enveloppe.db"

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def imports_dir(self) -> Path:
        return self.data_dir / "imports"


def _load_or_create_secret(data_dir: Path) -> str:
    """Retourne la clé de signature des sessions.

    Priorité à la variable d'environnement. À défaut, une clé aléatoire est
    générée une seule fois et conservée dans le volume de données, lisible
    par le seul propriétaire du processus.
    """
    from_env = os.environ.get("BUDGET_SECRET_KEY", "").strip()
    if from_env:
        return from_env

    key_file = data_dir / "secret.key"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()

    generated = secrets.token_urlsafe(48)
    key_file.touch(mode=0o600, exist_ok=True)
    os.chmod(key_file, 0o600)
    key_file.write_text(generated, encoding="utf-8")
    return generated


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("BUDGET_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "documents").mkdir(exist_ok=True)
    (data_dir / "imports").mkdir(exist_ok=True)

    return Settings(
        data_dir=data_dir,
        secret_key=_load_or_create_secret(data_dir),
        currency=os.environ.get("BUDGET_CURRENCY", "EUR"),
        session_max_age=_env_int("BUDGET_SESSION_MAX_AGE", 60 * 60 * 24 * 14),
        max_upload_mb=_env_int("BUDGET_MAX_UPLOAD_MB", 25),
        behind_proxy=_env_bool("BUDGET_BEHIND_PROXY", True),
    )


settings = load_settings()
