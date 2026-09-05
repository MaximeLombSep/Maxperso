"""Import des relevés bancaires (CSV et OFX/QFX).

Aucune connexion n'est établie vers la banque : l'utilisateur dépose le
fichier qu'il a lui-même téléchargé. Aucun identifiant bancaire n'est
demandé, ni stocké.
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import io
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Account, ImportBatch, ImportProfile, Transaction
from .categorizer import apply_rules, normalize_label
from .money import parse_amount

ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
DELIMITERS = (";", ",", "\t", "|")

DATE_FORMATS = (
    "%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y",
    "%Y/%m/%d", "%d %m %Y", "%m/%d/%Y",
)

HEADER_HINTS = {
    "date": ("date operation", "date de l operation", "date d operation",
             "date comptable", "date de comptabilisation", "date"),
    "value_date": ("date de valeur", "date valeur", "valeur"),
    "label": ("libelle", "libelle operation", "libelle simplifie", "description",
              "intitule", "nature de l operation", "nature", "motif", "detail"),
    "amount": ("montant", "montant de l operation", "montant eur", "amount",
               "montant (eur)"),
    "debit": ("debit", "debit euros", "retrait", "depense"),
    "credit": ("credit", "credit euros", "depot", "recette"),
}


@dataclass
class ParsedTx:
    op_date: date
    amount_cents: int
    label: str
    raw_label: str = ""
    value_date: date | None = None
    fitid: str | None = None


@dataclass
class ImportPreview:
    encoding: str
    delimiter: str
    header_row: int
    headers: list[str]
    sample: list[list[str]]
    mapping: dict[str, str]
    decimal_sep: str
    date_format: str
    amount_mode: str


@dataclass
class Suspect:
    """Quasi-doublon : même montant, date voisine, libellé très proche."""

    parsed: ParsedTx
    existing_id: int
    existing_date: date
    existing_label: str
    similarity: float


@dataclass
class ImportResult:
    total_rows: int = 0
    inserted: int = 0
    duplicates: int = 0
    errors: int = 0
    categorized: int = 0
    messages: list[str] = field(default_factory=list)
    suspects: list[Suspect] = field(default_factory=list)
    inserted_ids: list[int] = field(default_factory=list)
    batch_id: int | None = None

    @property
    def skipped_suspects(self) -> int:
        return len(self.suspects)


# --------------------------------------------------------------------------
# Décodage et détection
# --------------------------------------------------------------------------


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """Retourne (texte, encodage retenu). Repli garanti sur latin-1."""
    for encoding in ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1"


def detect_delimiter(text: str) -> str:
    """Le séparateur le plus régulier sur les 20 premières lignes non vides."""
    lines = [line for line in text.splitlines()[:20] if line.strip()]
    if not lines:
        return ";"
    best, best_score = ";", -1.0
    for delim in DELIMITERS:
        counts = [line.count(delim) for line in lines]
        if max(counts, default=0) == 0:
            continue
        median = sorted(counts)[len(counts) // 2]
        spread = sum(abs(c - median) for c in counts) / len(counts)
        score = median - spread
        if score > best_score:
            best, best_score = delim, score
    return best


def _slug(value: str) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _score_header(cells: list[str]) -> int:
    slugs = [_slug(c) for c in cells]
    score = 0
    for hints in HEADER_HINTS.values():
        if any(slug in hints or any(slug.startswith(h) for h in hints) for slug in slugs):
            score += 1
    return score


def find_header_row(rows: list[list[str]]) -> int:
    """Les exports bancaires précèdent souvent l'en-tête de lignes de garde."""
    best_index, best_score = 0, -1
    for index, row in enumerate(rows[:15]):
        score = _score_header(row)
        if score > best_score:
            best_index, best_score = index, score
    return best_index if best_score >= 2 else 0


def guess_mapping(headers: list[str]) -> dict[str, str]:
    slugs = {_slug(h): h for h in headers}
    mapping: dict[str, str] = {}
    for field_name, hints in HEADER_HINTS.items():
        for hint in hints:
            for slug, original in slugs.items():
                if slug == hint or slug.startswith(hint):
                    mapping[field_name] = original
                    break
            if field_name in mapping:
                break
    # « Date de valeur » ne doit pas être choisie comme date d'opération.
    if mapping.get("date") and mapping.get("date") == mapping.get("value_date"):
        for slug, original in slugs.items():
            if slug.startswith("date") and original != mapping["value_date"]:
                mapping["date"] = original
                break
    return mapping


def detect_decimal_sep(samples: list[str]) -> str:
    commas = sum(1 for s in samples if re.search(r",\d{1,2}\b", s or ""))
    dots = sum(1 for s in samples if re.search(r"\.\d{1,2}\b", s or ""))
    return "," if commas >= dots else "."


def detect_date_format(samples: list[str]) -> str:
    for fmt in DATE_FORMATS:
        ok = 0
        for sample in samples:
            if not sample:
                continue
            try:
                datetime.strptime(sample.strip()[:10], fmt)
                ok += 1
            except ValueError:
                pass
        if ok and ok >= max(1, len([s for s in samples if s]) // 2):
            return fmt
    return "%d/%m/%Y"


def parse_date(raw: str, preferred: str = "") -> date | None:
    if not raw:
        return None
    text = str(raw).strip()[:10]
    formats = ([preferred] if preferred else []) + list(DATE_FORMATS)
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def read_rows(text: str, delimiter: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [row for row in reader if any(cell.strip() for cell in row)]


def preview_csv(raw: bytes) -> ImportPreview:
    """Analyse un fichier sans rien écrire : sert à confirmer le mapping."""
    text, encoding = decode_bytes(raw)
    delimiter = detect_delimiter(text)
    rows = read_rows(text, delimiter)
    if not rows:
        raise ValueError("Fichier vide ou illisible.")

    header_index = find_header_row(rows)
    headers = [cell.strip() for cell in rows[header_index]]
    body = rows[header_index + 1 :]
    mapping = guess_mapping(headers)

    def column(name: str) -> list[str]:
        if name not in mapping or mapping[name] not in headers:
            return []
        idx = headers.index(mapping[name])
        return [row[idx] if idx < len(row) else "" for row in body[:40]]

    amount_mode = "single" if "amount" in mapping else "debit_credit"
    numeric_samples = column("amount") or column("debit") + column("credit")

    return ImportPreview(
        encoding=encoding,
        delimiter=delimiter,
        header_row=header_index,
        headers=headers,
        sample=[row[: len(headers)] for row in body[:8]],
        mapping=mapping,
        decimal_sep=detect_decimal_sep(numeric_samples),
        date_format=detect_date_format(column("date")),
        amount_mode=amount_mode,
    )


# --------------------------------------------------------------------------
# Lecture selon un profil
# --------------------------------------------------------------------------


def parse_csv(raw: bytes, profile: ImportProfile) -> tuple[list[ParsedTx], int, list[str]]:
    """Applique un profil de mapping. Retourne (opérations, lignes lues, erreurs)."""
    text = raw.decode(profile.encoding, errors="replace")
    rows = read_rows(text, profile.delimiter)
    if not rows:
        return [], 0, ["Fichier vide."]

    header_index = min(profile.skip_rows, len(rows) - 1)
    headers = [cell.strip() for cell in rows[header_index]]
    body = rows[header_index + 1 :]

    def index_of(column_name: str) -> int | None:
        if not column_name:
            return None
        if column_name in headers:
            return headers.index(column_name)
        slug_target = _slug(column_name)
        for i, head in enumerate(headers):
            if _slug(head) == slug_target:
                return i
        return None

    i_date = index_of(profile.col_date)
    i_label = index_of(profile.col_label)
    i_amount = index_of(profile.col_amount)
    i_debit = index_of(profile.col_debit)
    i_credit = index_of(profile.col_credit)
    i_value = index_of(profile.col_value_date)

    if i_date is None or i_label is None:
        return [], len(body), ["Colonnes date ou libellé introuvables."]

    parsed: list[ParsedTx] = []
    errors: list[str] = []

    for line_no, row in enumerate(body, start=header_index + 2):
        def cell(index: int | None) -> str:
            if index is None or index >= len(row):
                return ""
            return row[index].strip()

        op_date = parse_date(cell(i_date), profile.date_format)
        if op_date is None:
            errors.append(f"Ligne {line_no} : date illisible « {cell(i_date)} ».")
            continue

        if profile.amount_mode == "debit_credit":
            debit = parse_amount(cell(i_debit), profile.decimal_sep, profile.thousands_sep)
            credit = parse_amount(cell(i_credit), profile.decimal_sep, profile.thousands_sep)
            if debit is None and credit is None:
                errors.append(f"Ligne {line_no} : aucun montant.")
                continue
            amount = -abs(debit) if debit else 0
            if credit:
                amount += abs(credit)
        else:
            amount = parse_amount(cell(i_amount), profile.decimal_sep, profile.thousands_sep)
            if amount is None:
                errors.append(f"Ligne {line_no} : montant illisible.")
                continue

        if profile.invert_sign:
            amount = -amount

        label = cell(i_label)
        parsed.append(
            ParsedTx(
                op_date=op_date,
                amount_cents=amount,
                label=label[:255],
                raw_label=label,
                value_date=parse_date(cell(i_value), profile.date_format),
            )
        )

    return parsed, len(body), errors


_OFX_TAG = re.compile(r"<([A-Z0-9.]+)>([^<\r\n]*)", re.IGNORECASE)
_OFX_TRN = re.compile(r"<STMTTRN>(.*?)</STMTTRN>", re.IGNORECASE | re.DOTALL)


def _ofx_date(raw: str) -> date | None:
    text = re.sub(r"[^0-9]", "", raw or "")[:8]
    if len(text) < 8:
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def parse_ofx(raw: bytes) -> tuple[list[ParsedTx], int, list[str]]:
    """Lecteur OFX/QFX tolérant : SGML (OFX 1.x) comme XML (OFX 2.x)."""
    text, _ = decode_bytes(raw)
    blocks = _OFX_TRN.findall(text)
    if not blocks:
        return [], 0, ["Aucune opération <STMTTRN> trouvée dans le fichier OFX."]

    parsed: list[ParsedTx] = []
    errors: list[str] = []

    for index, block in enumerate(blocks, start=1):
        fields = {tag.upper(): value.strip() for tag, value in _OFX_TAG.findall(block)}
        op_date = _ofx_date(fields.get("DTPOSTED", ""))
        amount = parse_amount(fields.get("TRNAMT", ""), decimal_sep=".", thousands_sep="")
        if op_date is None or amount is None:
            errors.append(f"Opération {index} : date ou montant absent.")
            continue

        name = fields.get("NAME", "")
        memo = fields.get("MEMO", "")
        label = " — ".join(part for part in (name, memo) if part) or fields.get(
            "TRNTYPE", "Opération"
        )
        parsed.append(
            ParsedTx(
                op_date=op_date,
                amount_cents=amount,
                label=label[:255],
                raw_label=label,
                value_date=_ofx_date(fields.get("DTUSER", "")),
                fitid=(fields.get("FITID") or None),
            )
        )

    return parsed, len(blocks), errors


# --------------------------------------------------------------------------
# Anti-doublon et insertion
# --------------------------------------------------------------------------


def fingerprint(
    op_date: date, amount_cents: int, normalized: str, occurrence: int, fitid: str | None
) -> str:
    """Empreinte d'unicité d'une opération dans un compte.

    Le FITID de la banque prime quand il existe (identifiant stable). Sinon on
    combine date + montant + libellé normalisé + rang d'occurrence, ce qui
    laisse passer deux achats identiques le même jour tout en bloquant le
    réimport du même relevé.
    """
    if fitid:
        return hashlib.sha256(f"fitid:{fitid}".encode("utf-8")).hexdigest()
    payload = f"{op_date.isoformat()}|{amount_cents}|{normalized}|{occurrence}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


NEAR_DUPLICATE_DAYS = 5
NEAR_DUPLICATE_RATIO = 0.55


def _similarity(left: str, right: str) -> float:
    """Proximité de deux libellés normalisés, entre 0 et 1."""
    if not left or not right:
        return 0.0
    if left == right or left in right or right in left:
        return 1.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def find_near_duplicate(
    existing: dict[int, list[tuple[int, date, str]]],
    op_date: date,
    amount_cents: int,
    normalized: str,
) -> tuple[int, date, str, float] | None:
    """Cherche une opération déjà en base que celle-ci recopierait.

    Le montant doit être identique au centime : c'est la seule contrainte
    stricte. La date peut différer de quelques jours (date d'opération contre
    date de valeur selon la source) et le libellé varier d'une source à
    l'autre — un relevé PDF n'écrit pas les libellés comme un export CSV.
    """
    best = None
    for tx_id, other_date, other_label in existing.get(amount_cents, ()):
        if abs((other_date - op_date).days) > NEAR_DUPLICATE_DAYS:
            continue
        score = _similarity(normalized, other_label)
        if score >= NEAR_DUPLICATE_RATIO and (best is None or score > best[3]):
            best = (tx_id, other_date, other_label, score)
    return best


def ingest(
    db: Session,
    account: Account,
    parsed: list[ParsedTx],
    filename: str,
    source_format: str,
    total_rows: int,
    errors: list[str],
    profile_id: int | None = None,
    strict_duplicates: bool = True,
) -> ImportResult:
    """Insère les opérations non déjà présentes, puis applique les règles.

    Deux filets successifs contre les doublons :

      1. l'**empreinte exacte** (FITID de la banque, ou date + montant +
         libellé normalisé + rang d'occurrence) écarte le réimport d'un même
         relevé ;
      2. la **détection de quasi-doublon** écarte la même opération arrivée
         par une autre source — un PDF après un CSV, par exemple, où la date
         retenue et l'orthographe du libellé diffèrent.

    Le second filet ne supprime rien en silence : les opérations écartées
    sont listées dans le résultat, avec l'opération existante qu'elles
    recopient. `strict_duplicates=False` les insère malgré tout, pour les cas
    où deux dépenses identiques sont réellement distinctes.
    """
    result = ImportResult(total_rows=total_rows, errors=len(errors), messages=errors[:20])

    batch = ImportBatch(
        account_id=account.id,
        filename=filename[:255],
        source_format=source_format,
        profile_id=profile_id,
        total_rows=total_rows,
    )
    db.add(batch)
    db.flush()

    known_fingerprints = set(
        db.scalars(
            select(Transaction.fingerprint).where(Transaction.account_id == account.id)
        )
    )

    # Index des opérations déjà en base, par montant. Constitué avant la
    # boucle : les lignes insérées par cet import ne s'y ajoutent pas, sans
    # quoi deux achats identiques du même relevé se neutraliseraient.
    existing_by_amount: dict[int, list[tuple[int, date, str]]] = defaultdict(list)
    for tx_id, tx_date, tx_amount, tx_label in db.execute(
        select(
            Transaction.id,
            Transaction.op_date,
            Transaction.amount_cents,
            Transaction.normalized_label,
        ).where(Transaction.account_id == account.id)
    ).all():
        existing_by_amount[tx_amount].append((tx_id, tx_date, tx_label or ""))
    # Le rang d'occurrence est recompté depuis zéro à chaque lecture de
    # fichier : un même relevé réimporté produit exactement les mêmes
    # empreintes, donc aucun doublon, tandis que deux achats identiques le
    # même jour reçoivent des rangs différents et sont tous deux conservés.
    occurrences: dict[tuple, int] = defaultdict(int)
    fresh: list[Transaction] = []

    for item in sorted(parsed, key=lambda t: t.op_date):
        normalized = normalize_label(item.raw_label or item.label)
        key = (item.op_date, item.amount_cents, normalized)
        occurrences[key] += 1
        digest = fingerprint(
            item.op_date, item.amount_cents, normalized, occurrences[key], item.fitid
        )
        if digest in known_fingerprints:
            result.duplicates += 1
            continue

        near = find_near_duplicate(
            existing_by_amount, item.op_date, item.amount_cents, normalized
        )
        if near is not None:
            tx_id, other_date, other_label, score = near
            result.suspects.append(
                Suspect(
                    parsed=item,
                    existing_id=tx_id,
                    existing_date=other_date,
                    existing_label=other_label,
                    similarity=score,
                )
            )
            if strict_duplicates:
                occurrences[key] -= 1
                continue

        known_fingerprints.add(digest)
        transaction = Transaction(
            account_id=account.id,
            op_date=item.op_date,
            value_date=item.value_date,
            amount_cents=item.amount_cents,
            label=item.label,
            raw_label=item.raw_label or item.label,
            normalized_label=normalized[:255],
            kind="income" if item.amount_cents > 0 else "expense",
            fitid=item.fitid,
            fingerprint=digest,
            import_batch_id=batch.id,
        )
        db.add(transaction)
        fresh.append(transaction)
        result.inserted += 1

    batch.inserted = result.inserted
    # Du point de vue de l'historique, une opération écartée l'est : que ce
    # soit sur empreinte exacte ou sur quasi-doublon. Le détail des seconds
    # figure dans le compte rendu d'import.
    batch.duplicates = result.duplicates + (
        result.skipped_suspects if strict_duplicates else 0
    )
    batch.errors = result.errors
    db.commit()

    result.categorized = apply_rules(db, fresh)
    result.inserted_ids = [tx.id for tx in fresh]
    result.batch_id = batch.id
    return result


def detect_transfers(db: Session, window_days: int = 4) -> int:
    """Apparie les virements internes entre deux comptes suivis.

    Deux opérations de montants opposés, sur deux comptes différents, à
    quelques jours d'intervalle, sont marquées `transfer` : elles ne doivent
    peser ni sur les dépenses, ni sur les revenus du mois.
    """
    candidates = list(
        db.scalars(
            select(Transaction)
            .where(Transaction.kind != "transfer", Transaction.envelope_id.is_(None))
            .order_by(Transaction.op_date)
        )
    )
    by_amount: dict[int, list[Transaction]] = defaultdict(list)
    for tx in candidates:
        by_amount[tx.amount_cents].append(tx)

    paired = 0
    used: set[int] = set()
    for tx in candidates:
        if tx.id in used or tx.amount_cents >= 0:
            continue
        for other in by_amount.get(-tx.amount_cents, []):
            if other.id in used or other.account_id == tx.account_id:
                continue
            if abs((other.op_date - tx.op_date).days) > window_days:
                continue
            group = str(uuid.uuid4())
            tx.kind = other.kind = "transfer"
            tx.transfer_group = other.transfer_group = group
            used.update({tx.id, other.id})
            paired += 1
            break

    if paired:
        db.commit()
    return paired


def default_profile(name: str, preview: ImportPreview) -> ImportProfile:
    """Profil pré-rempli à partir d'une analyse de fichier."""
    mapping = preview.mapping
    return ImportProfile(
        name=name,
        delimiter=preview.delimiter,
        encoding=preview.encoding,
        date_format=preview.date_format,
        decimal_sep=preview.decimal_sep,
        thousands_sep=" " if preview.decimal_sep == "," else ",",
        skip_rows=preview.header_row,
        amount_mode=preview.amount_mode,
        col_date=mapping.get("date", ""),
        col_label=mapping.get("label", ""),
        col_amount=mapping.get("amount", ""),
        col_debit=mapping.get("debit", ""),
        col_credit=mapping.get("credit", ""),
        col_value_date=mapping.get("value_date", ""),
    )
