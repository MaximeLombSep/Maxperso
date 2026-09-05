"""Lecture des relevés bancaires au format PDF.

Un PDF n'est pas un format d'échange : c'est une mise en page. Les colonnes
« débit » et « crédit » n'y existent que par leur position à l'écran. Ce
module s'appuie donc sur l'extraction en mode *layout* de pypdf, qui
conserve les espacements, puis :

  1. repère la ligne d'en-tête et la position de chaque colonne de montant ;
  2. classe chaque montant selon qu'il tombe à gauche ou à droite de la
     frontière entre ces colonnes ;
  3. rattache les lignes de continuation au libellé précédent ;
  4. reconstitue l'année à partir de la période du relevé.

C'est fiable sur les relevés à colonnes séparées, moins sur les mises en
page exotiques : l'écran de vérification affiche donc le total lu, à
comparer avec le relevé avant d'insérer quoi que ce soit. Le CSV et l'OFX
restent préférables quand la banque les propose.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO

from .importer import ParsedTx
from .money import parse_amount

# Montant français : « 1 234,56 », « 87,45 », éventuellement signé.
AMOUNT_RE = re.compile(
    "[-+]?[ \u00a0\u202f]{0,3}"           # signe, parfois séparé du nombre
    "\\d{1,3}(?:[ \u00a0\u202f]\\d{3})*"      # milliers séparés par une espace
    ",\\d{2}(?![\\d,])"
)
# Une opération commence par une date suivie d'une espace : « 18/11/2026) »
# au fil d'un paragraphe de conditions générales n'en est pas une.
DATE_START_RE = re.compile(r"^\s*(\d{2})[/.](\d{2})(?:[/.](\d{2,4}))?(?=\s|$)")
PERIOD_RE = re.compile(
    r"du\s+(\d{2})[/.](\d{2})[/.](\d{4})\s+au\s+(\d{2})[/.](\d{2})[/.](\d{4})",
    re.IGNORECASE,
)
# Certains relevés n'annoncent qu'une date de clôture (« Relevé n° 53 au ... »).
CLOSING_RE = re.compile(r"\bau\s+(\d{2})[/.](\d{2})[/.](\d{4})", re.IGNORECASE)

# Deuxième colonne de date (date de valeur) collée à la date d'opération.
SECOND_DATE_RE = re.compile(r"^\s*(\d{2})[/.](\d{2})[/.](\d{2,4})\b")

# Bloc « paiements différés » : le détail des achats précède son en-tête, qui
# porte le numéro de carte, puis la date et le montant du prélèvement.
CARD_HEADER_RE = re.compile(
    r"PAIEMENTS?\s+DIFFERES?\s+carte\s+bancaire\s+N[\u00b0o\u00ba.]?\s*(\d+)\s*(.*)$",
    re.IGNORECASE,
)
CARD_TOTAL_RE = re.compile(
    r"montant\s+pr[ée]lev[ée]\s+le\s+(\d{2})[/.](\d{2})(?:[/.](\d{2,4}))?\s*:?\s*"
    r"([\d\s  ]+,\d{2})",
    re.IGNORECASE,
)
# Achat porté par une carte différée : « CB INTERMARCHE  FACT 090726 ».
CARD_LINE_RE = re.compile(r"\bCB\b.*?\bFACT\s+(\d{2})(\d{2})(\d{2})\b", re.IGNORECASE)

# Lignes de synthèse : ce sont des soldes, pas des opérations.
SUMMARY_RE = re.compile(
    r"^\s*(?:ANCIEN\s+SOLDE|NOUVEAU\s+SOLDE|SOLDE\b|TOTAL\s+DES|TOTAUX|REPORT\b|"
    r"SOUS[- ]TOTAL|A\s+NOUVEAU)",
    re.IGNORECASE,
)
NOISE_RE = re.compile(
    r"^\s*(?:PAGE\s+\d|IBAN\b|BIC\s*:|WWW\.|RELEVE\s+D|IDENTIFIANT|N\.?\s*CLIENT|"
    r"DETAIL\s+DES\s+OPERATIONS|COMPTE\s+(?:DE\s+DEPOT|COURANT|CHEQUE)|SYNTHESE|"
    r"RELEV[E\u00c9]\s+N|VOTRE\s+RELEV[E\u00c9]|GA[+\s]NS|"
    r"CAISSE\s+D.EPARGNE|CONSEIL\s+D.ORIENTATION|ORIAS|"
    r"FRAIS\s+BANCAIRES\s+ET\s+COTISATIONS|"
    r"(?:VIREMENTS?|PRELEVEMENTS?|PAIEMENTS?|CHEQUES?|REMISES?|AUTRES)\s+"
    r"(?:RECUS?|EMIS|CARTE|OPERATIONS|BANCAIRES?)?\s*$)",
    re.IGNORECASE,
)

DEBIT_WORDS = ("debit", "retrait", "depense")
CREDIT_WORDS = ("credit", "depot", "recette")


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


@dataclass
class CardBlock:
    """Détail des achats d'une carte à débit différé, tel qu'imprimé.

    Le relevé liste les achats à la date du prélèvement, en indiquant la vraie
    date d'achat sous la forme « FACT jjmmaa ». C'est cette date-là qui compte
    pour le budget : un achat du 9 juillet appartient à juillet, même si
    l'argent part le 4 août.
    """

    card_number: str
    holder: str
    settlement_on: date
    announced_cents: int
    purchases: list[ParsedTx] = field(default_factory=list)

    @property
    def parsed_cents(self) -> int:
        return sum(item.amount_cents for item in self.purchases)

    @property
    def matches(self) -> bool:
        """Le détail lu recompose-t-il le total annoncé par la banque ?"""
        return self.parsed_cents == -self.announced_cents

    @property
    def label(self) -> str:
        return f"Carte n° {self.card_number}" + (f" — {self.holder}" if self.holder else "")


@dataclass
class PdfPreview:
    parsed: list[ParsedTx]
    lines_read: int
    warnings: list[str] = field(default_factory=list)
    period_start: date | None = None
    period_end: date | None = None
    columns: str = "signed"  # debit_credit | signed
    sample_lines: list[str] = field(default_factory=list)
    card_blocks: list[CardBlock] = field(default_factory=list)

    @property
    def total_cents(self) -> int:
        return sum(item.amount_cents for item in self.parsed)

    @property
    def debit_cents(self) -> int:
        return sum(item.amount_cents for item in self.parsed if item.amount_cents < 0)

    @property
    def credit_cents(self) -> int:
        return sum(item.amount_cents for item in self.parsed if item.amount_cents > 0)


def extract_lines(raw: bytes) -> list[str]:
    """Lignes du PDF, espacements conservés (mode layout)."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dépendance déclarée
        raise ValueError(
            "La lecture des PDF nécessite pypdf : ajoutez-le aux dépendances."
        ) from exc

    try:
        reader = PdfReader(BytesIO(raw))
    except Exception as exc:
        raise ValueError(f"PDF illisible : {exc}") from exc

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ValueError(
                "PDF protégé par mot de passe : retirez la protection avant l'import."
            ) from exc

    lines: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except Exception:
            text = page.extract_text() or ""
        lines.extend(text.splitlines())
    return lines


def find_columns(lines: list[str]) -> tuple[int | None, int | None]:
    """Position des colonnes débit et crédit dans la ligne d'en-tête."""
    for line in lines:
        folded = _fold(line)
        debit_at = next(
            (folded.index(word) for word in DEBIT_WORDS if word in folded), None
        )
        credit_at = next(
            (folded.index(word) for word in CREDIT_WORDS if word in folded), None
        )
        if debit_at is not None and credit_at is not None and debit_at != credit_at:
            return debit_at, credit_at
    return None, None


def find_period(lines: list[str]) -> tuple[date | None, date | None]:
    for line in lines[:80]:
        match = PERIOD_RE.search(line)
        if match:
            day1, month1, year1, day2, month2, year2 = (int(v) for v in match.groups())
            try:
                return date(year1, month1, day1), date(year2, month2, day2)
            except ValueError:
                return None, None

    # À défaut, la date de clôture suffit : le relevé couvre le mois qui la
    # précède, ce qui permet de dater les lignes écrites sans millésime.
    for line in lines[:80]:
        match = CLOSING_RE.search(line)
        if match:
            day, month, year = (int(v) for v in match.groups())
            try:
                end = date(year, month, day)
            except ValueError:
                continue
            start_month = end.month - 1 or 12
            start_year = end.year if end.month > 1 else end.year - 1
            return date(start_year, start_month, 1), end
    return None, None


def _fact_date(label: str, reference: date | None) -> date | None:
    """Date réelle d'achat portée par « FACT jjmmaa »."""
    match = CARD_LINE_RE.search(label)
    if match is None:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(2000 + year, month, day)
    except ValueError:
        return None


def resolve_year(
    day: int, month: int, start: date | None, end: date | None, fallback: int
) -> int:
    """Année d'une date écrite sans millésime, d'après la période du relevé."""
    if start is None:
        return fallback
    for year in {start.year, (end or start).year}:
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if end is None or start <= candidate <= end:
            return year
    # Hors période (opération reportée) : on suit le mois de début de relevé.
    return start.year + 1 if month < start.month else start.year


def preview_pdf(raw: bytes, unsigned_as_debit: bool = True) -> PdfPreview:
    """Analyse complète sans rien écrire : sert à valider avant insertion."""
    lines = extract_lines(raw)
    if not lines:
        raise ValueError("Aucun texte extractible : ce PDF est probablement une image "
                         "scannée. Demandez un export CSV ou OFX à votre banque.")

    debit_at, credit_at = find_columns(lines)
    boundary = (
        (debit_at + credit_at) / 2 if debit_at is not None and credit_at is not None else None
    )
    start, end = find_period(lines)
    fallback_year = (start or date.today()).year

    parsed: list[ParsedTx] = []
    warnings: list[str] = []
    blocks: list[CardBlock] = []
    current_block: CardBlock | None = None
    merge_target: ParsedTx | None = None
    pending_number, pending_holder = "", ""
    read = 0
    unsigned = 0

    def open_block(match: re.Match) -> CardBlock:
        """Ouvre un bloc de carte : l'en-tête précède le détail des achats."""
        nonlocal pending_number, pending_holder

        day, month, year_text, amount_text = match.groups()
        year = int(year_text) if year_text else None
        if year is not None and year < 100:
            year += 2000
        if year is None:
            year = resolve_year(int(day), int(month), start, end, fallback_year)
        try:
            settled_on = date(year, int(month), int(day))
        except ValueError:
            settled_on = end or date.today()

        announced = parse_amount(amount_text, decimal_sep=",", thousands_sep=" ") or 0
        block = CardBlock(
            card_number=pending_number,
            holder=pending_holder,
            settlement_on=settled_on,
            announced_cents=abs(announced),
        )
        blocks.append(block)

        # Le compte est bien débité une fois du total : cette écriture sera
        # rapprochée des achats pour rester neutre dans le budget.
        libelle = f"PAIEMENTS DIFFERES CARTE N° {pending_number}".strip()
        parsed.append(
            ParsedTx(
                op_date=settled_on,
                amount_cents=-block.announced_cents,
                label=libelle[:255],
                raw_label=libelle,
            )
        )
        pending_number, pending_holder = "", ""
        return block

    for line in lines:
        if not line.strip() or NOISE_RE.match(line):
            continue

        header = CARD_HEADER_RE.search(line)
        if header:
            pending_number = header.group(1)
            pending_holder = re.sub(r"\s{2,}", " ", header.group(2)).strip()[:80]
            merge_target = None
            continue

        total = CARD_TOTAL_RE.search(line)
        if total:
            current_block = open_block(total)
            merge_target = None
            continue

        if SUMMARY_RE.match(line):
            merge_target = None
            continue

        date_match = DATE_START_RE.match(line)
        amounts = list(AMOUNT_RE.finditer(line))

        if date_match is None:
            # Ligne de continuation : elle complète le libellé de l'opération
            # précédente — jamais celui d'un règlement reconstitué, ni d'une
            # ligne de mobilier déjà écartée.
            if merge_target is not None and not amounts and len(line.strip()) > 3:
                merged = f"{merge_target.raw_label} {line.strip()}".strip()[:500]
                merge_target.raw_label = merged
                merge_target.label = merged[:255]
            continue

        read += 1
        if not amounts:
            warnings.append(f"Ligne sans montant ignorée : « {line.strip()[:70]} »")
            continue

        day, month, year_text = date_match.groups()
        year = int(year_text) if year_text else None
        if year is not None and year < 100:
            year += 2000
        if year is None:
            year = resolve_year(int(day), int(month), start, end, fallback_year)

        try:
            op_date = date(year, int(month), int(day))
        except ValueError:
            warnings.append(f"Date invalide ignorée : « {line.strip()[:70]} »")
            continue

        chosen = amounts[-1] if len(amounts) > 1 else amounts[0]
        value = parse_amount(chosen.group(), decimal_sep=",", thousands_sep=" ")
        if value is None:
            warnings.append(f"Montant illisible ignoré : « {line.strip()[:70]} »")
            continue

        if boundary is not None:
            # La colonne se juge sur les chiffres : le signe et l'espace qui
            # le sépare du nombre décaleraient le repère vers la gauche.
            raw = chosen.group()
            lead = len(raw) - len(raw.lstrip(" +-\u00a0\u202f"))
            middle = (chosen.start() + lead + chosen.end()) / 2
            signed = abs(value) if middle > boundary else -abs(value)
        elif chosen.group().strip().startswith(("-", "+")):
            signed = value
        else:
            unsigned += 1
            signed = -abs(value) if unsigned_as_debit else abs(value)

        rest = line[date_match.end() : chosen.start()]

        # Deuxième colonne de date (date de valeur) : elle ne fait pas partie
        # du libellé.
        value_date = None
        second = SECOND_DATE_RE.match(rest)
        if second:
            vday, vmonth, vyear_text = second.groups()
            vyear = int(vyear_text) if vyear_text else year
            if vyear < 100:
                vyear += 2000
            try:
                value_date = date(vyear, int(vmonth), int(vday))
            except ValueError:
                value_date = None
            rest = rest[second.end() :]

        label = re.sub(r"\s{2,}", " ", rest).strip() or "Opération"

        item = ParsedTx(
            op_date=op_date,
            amount_cents=signed,
            label=label[:255],
            raw_label=label,
            value_date=value_date,
        )

        # Achat de carte différée : il rejoint le bloc ouvert, daté du jour
        # réel de l'achat plutôt que du jour du prélèvement.
        purchase_date = _fact_date(label, op_date)
        if purchase_date is not None and current_block is not None:
            item.op_date = purchase_date
            item.value_date = op_date
            current_block.purchases.append(item)
            merge_target = None
            continue

        parsed.append(item)
        merge_target = item

    for block in blocks:
        if not block.matches:
            warnings.append(
                f"{block.label} : le détail lu ({-block.parsed_cents / 100:.2f} €) ne "
                f"recompose pas le prélèvement annoncé ({block.announced_cents / 100:.2f} €). "
                "Vérifiez avant d'importer."
            )

    if boundary is None and unsigned:
        warnings.insert(
            0,
            f"{unsigned} montant(s) sans signe ni colonne débit/crédit : traités comme "
            + ("des débits" if unsigned_as_debit else "des crédits")
            + ". Vérifiez le sens avant d'importer.",
        )

    return PdfPreview(
        parsed=parsed,
        card_blocks=blocks,
        lines_read=read,
        warnings=warnings,
        period_start=start,
        period_end=end,
        columns="debit_credit" if boundary is not None else "signed",
        sample_lines=[line for line in lines[:40] if line.strip()][:20],
    )


def parse_pdf(
    raw: bytes, unsigned_as_debit: bool = True
) -> tuple[list[ParsedTx], int, list[str]]:
    """Signature alignée sur `parse_csv` / `parse_ofx`."""
    preview = preview_pdf(raw, unsigned_as_debit)
    return preview.parsed, preview.lines_read, preview.warnings
