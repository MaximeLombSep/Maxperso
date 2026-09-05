"""Manipulation des montants — centimes entiers uniquement."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_CLEAN = re.compile(r"[^\d,.\-+()]")


def parse_amount(
    raw: str, decimal_sep: str = ",", thousands_sep: str = " "
) -> int | None:
    """Convertit un montant texte en centimes.

    Tolère les formats bancaires français : « 1 234,56 », « -1.234,56 »,
    « (45,00) » pour un débit, « 45,00 € », « +12,30 ».
    Retourne `None` si la cellule est vide ou illisible.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    text = _CLEAN.sub("", text).replace("(", "").replace(")", "")
    if not text:
        return None

    if thousands_sep and thousands_sep != decimal_sep:
        text = text.replace(thousands_sep, "")
    text = text.replace(" ", "").replace(" ", "")

    if decimal_sep == ",":
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "")

    if text in {"", "-", "+", "."}:
        return None

    try:
        value = Decimal(text)
    except InvalidOperation:
        return None

    cents = int((value * 100).to_integral_value(rounding="ROUND_HALF_UP"))
    return -abs(cents) if negative else cents


def format_cents(cents: int | None, with_symbol: bool = True) -> str:
    """Rend « 1 234,56 € » (espace insécable comme séparateur de milliers)."""
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    units, rest = divmod(abs(int(cents)), 100)
    grouped = f"{units:,}".replace(",", " ")
    out = f"{sign}{grouped},{rest:02d}"
    return f"{out} €" if with_symbol else out


def format_cents_short(cents: int | None) -> str:
    """Version compacte pour les graphiques : « 1,2 k€ »."""
    if cents is None:
        return "—"
    units = cents / 100
    if abs(units) >= 10_000:
        return f"{units / 1000:.0f} k€"
    if abs(units) >= 1_000:
        return f"{units / 1000:.1f}".replace(".", ",") + " k€"
    return f"{units:.0f} €"


def euros_to_cents(value: str | float | None) -> int:
    """Saisie utilisateur (formulaire) vers centimes ; 0 si vide."""
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return int(round(float(value) * 100))
    parsed = parse_amount(str(value))
    return parsed if parsed is not None else 0
