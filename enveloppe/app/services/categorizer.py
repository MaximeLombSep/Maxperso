"""Normalisation des libellés et catégorisation automatique.

Deux mécanismes se complètent :
  1. des **règles** explicites (table `rules`), triées par priorité ;
  2. l'**apprentissage** : chaque affectation manuelle propose une règle
     dérivée du libellé, que l'utilisateur valide.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Envelope, Rule, Transaction

# Bruit propre aux libellés bancaires français : n'aide pas à identifier
# le commerçant, mais fait échouer les rapprochements s'il reste en place.
_NOISE_PATTERNS = [
    r"\bCARTE\s+\d{2}[/.]\d{2}[/.]\d{2,4}\b",
    r"\bCB\s*\*{0,4}\d{2,4}\b",
    r"\bACHAT\b",
    r"\bPAIEMENT\s+(PAR\s+)?CARTE\b",
    r"\bFACTURE\s+CARTE\b",
    r"\bVIR(EMENT)?(\s+SEPA)?(\s+(RECU|EMIS|INST))?\b",
    r"\bPRLV(\s+SEPA)?\b",
    r"\bPRELEVEMENT(\s+SEPA)?\b",
    r"\bMANDAT\s+\S+\b",
    r"\bREF\s*:?\s*\S+\b",
    r"\bECH(EANCE)?\s*\d*\b",
    r"\bDU\s+\d{2}[/.]\d{2}[/.]\d{2,4}\b",
    r"\b\d{2}[/.]\d{2}[/.]\d{2,4}\b",
    r"\b\d{6,}\b",
    r"\bX{2,}\d*\b",
    r"\*+\d{0,6}",
]
_NOISE_RE = re.compile("|".join(_NOISE_PATTERNS))
_NON_WORD = re.compile(r"[^A-Z0-9]+")
_SPACES = re.compile(r"\s+")

_STOPWORDS = {
    "SARL", "SAS", "SA", "EURL", "SASU", "FR", "PARIS", "LYON", "CEDEX",
    "COM", "WWW", "HTTP", "HTTPS", "LE", "LA", "LES", "DE", "DU", "DES",
    "ET", "AU", "AUX", "SUR", "POUR", "PAR", "AVEC", "SEPA", "INST",
}


def normalize_label(label: str) -> str:
    """Forme canonique d'un libellé, utilisée pour les règles et l'anti-doublon."""
    if not label:
        return ""
    text = unicodedata.normalize("NFKD", label)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.upper()
    text = _NOISE_RE.sub(" ", text)
    text = _NON_WORD.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


# Tout ce qui suit ces marqueurs est de la référence bancaire : identifiant
# de mandat, numéro de créancier, référence du donneur d'ordre. Rien qui
# identifie le commerçant, et beaucoup de bruit pour les regroupements.
_CUT_RE = re.compile(
    r"\bREF\W*(?:DONNEUR|DU\s+MANDAT|MANDAT)"
    r"|\bREFERENCE\b|\bCTTO\b|\bEFECTO\b|\bID\s+CREANCIER\b|\bRUM\b"
)

# Marqueurs propres aux relevés de carte, en plus du bruit commun. « FACT
# jjmmaa » date l'achat sur un relevé de carte différée : utile au moment de
# lire le PDF, sans intérêt pour reconnaître le commerçant.
_MERCHANT_NOISE = re.compile(r"\bFACT\s*\d{2,8}\b|\bFACT\b|\bCB\b")


def merchant_label(label: str) -> str:
    """Libellé réduit au commerçant, pour proposer des règles.

    Volontairement distinct de `normalize_label`, qui alimente l'empreinte
    anti-doublon : changer la forme canonique d'un libellé déjà en base
    ferait réapparaître toutes les opérations au prochain import. Ce
    nettoyage-ci n'a donc aucun effet sur les empreintes existantes, et les
    règles produites restent compatibles puisqu'elles sont recherchées en
    « contient » dans le libellé normalisé.
    """
    if not label:
        return ""
    text = unicodedata.normalize("NFKD", label)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.upper()

    cut = _CUT_RE.search(text)
    if cut:
        text = text[: cut.start()]

    text = _MERCHANT_NOISE.sub(" ", text)
    text = _NOISE_RE.sub(" ", text)
    text = _NON_WORD.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def merchant_tokens(label: str, limit: int = 3) -> list[str]:
    """Mots significatifs d'un libellé, dans l'ordre d'apparition."""
    tokens = [
        tok
        for tok in merchant_label(label).split()
        if len(tok) > 2 and tok not in _STOPWORDS and not tok.isdigit()
    ]
    return tokens[:limit]


def suggest_pattern(label: str) -> str:
    """Motif proposé pour créer une règle à partir d'une opération."""
    tokens = merchant_tokens(label, limit=2)
    return " ".join(tokens) if tokens else normalize_label(label)[:40]


@dataclass
class RuleMatch:
    envelope_id: int
    rule_id: int


def _rule_applies(rule: Rule, normalized: str, amount_cents: int) -> bool:
    if not rule.enabled:
        return False
    if rule.sign == "debit" and amount_cents > 0:
        return False
    if rule.sign == "credit" and amount_cents < 0:
        return False
    magnitude = abs(amount_cents)
    if rule.min_cents is not None and magnitude < rule.min_cents:
        return False
    if rule.max_cents is not None and magnitude > rule.max_cents:
        return False

    pattern = rule.pattern.upper().strip()
    if rule.matcher == "prefix":
        return normalized.startswith(pattern)
    if rule.matcher == "regex":
        try:
            return re.search(rule.pattern, normalized, re.IGNORECASE) is not None
        except re.error:
            return False
    return pattern in normalized


def load_rules(db: Session) -> list[Rule]:
    return list(
        db.scalars(
            select(Rule).where(Rule.enabled.is_(True)).order_by(Rule.priority, Rule.id)
        )
    )


def match(rules: list[Rule], label: str, amount_cents: int) -> RuleMatch | None:
    normalized = normalize_label(label)
    for rule in rules:
        if _rule_applies(rule, normalized, amount_cents):
            return RuleMatch(envelope_id=rule.envelope_id, rule_id=rule.id)
    return None


def apply_rules(
    db: Session, transactions: list[Transaction], only_uncategorized: bool = True
) -> int:
    """Affecte une enveloppe aux opérations qui matchent une règle.

    Retourne le nombre d'opérations catégorisées. Ne touche jamais une
    affectation faite à la main (`reviewed=True`).
    """
    rules = load_rules(db)
    if not rules:
        return 0

    touched = 0
    for tx in transactions:
        if only_uncategorized and tx.envelope_id is not None:
            continue
        if tx.reviewed:
            continue
        if tx.kind == "transfer":
            # Un mouvement entre deux de vos comptes n'est ni une dépense ni
            # une recette : lui attribuer une enveloppe n'aurait aucun sens.
            continue
        found = match(rules, tx.raw_label or tx.label, tx.amount_cents)
        if found is None:
            continue
        tx.envelope_id = found.envelope_id
        rule = db.get(Rule, found.rule_id)
        if rule is not None:
            rule.hits += 1
        touched += 1
    if touched:
        db.commit()
    return touched


def learn_from_assignment(
    db: Session, tx: Transaction, envelope_id: int, priority: int = 200
) -> Rule | None:
    """Crée (ou renforce) une règle après une affectation manuelle."""
    pattern = suggest_pattern(tx.raw_label or tx.label)
    if len(pattern) < 3:
        return None

    existing = db.scalar(
        select(Rule).where(Rule.pattern == pattern, Rule.matcher == "contains")
    )
    if existing is not None:
        existing.envelope_id = envelope_id
        existing.enabled = True
        db.commit()
        return existing

    rule = Rule(
        envelope_id=envelope_id,
        matcher="contains",
        pattern=pattern,
        sign="debit" if tx.amount_cents < 0 else "credit",
        priority=priority,
        auto_learned=True,
    )
    db.add(rule)
    db.commit()
    return rule


# Amorce livrée avec l'application : couvre les libellés les plus courants
# des banques françaises. Chaque entrée est (motif, nom d'enveloppe).
SEED_RULES: list[tuple[str, str]] = [
    ("LOYER", "Loyer / Crédit"),
    ("CREDIT IMMOBILIER", "Loyer / Crédit"),
    ("EDF", "Énergie"),
    ("ENGIE", "Énergie"),
    ("TOTALENERGIES", "Énergie"),
    ("VEOLIA", "Eau"),
    ("SUEZ", "Eau"),
    ("SAUR", "Eau"),
    ("ORANGE", "Téléphone / Internet"),
    ("FREE", "Téléphone / Internet"),
    ("SFR", "Téléphone / Internet"),
    ("BOUYGUES", "Téléphone / Internet"),
    ("CARREFOUR", "Courses"),
    ("LECLERC", "Courses"),
    ("INTERMARCHE", "Courses"),
    ("SUPER U", "Courses"),
    ("LIDL", "Courses"),
    ("ALDI", "Courses"),
    ("AUCHAN", "Courses"),
    ("CASINO", "Courses"),
    ("MONOPRIX", "Courses"),
    ("PICARD", "Courses"),
    ("BOULANGERIE", "Courses"),
    ("TOTAL", "Carburant"),
    ("ESSO", "Carburant"),
    ("BP ", "Carburant"),
    ("SHELL", "Carburant"),
    ("STATION", "Carburant"),
    ("SNCF", "Transports"),
    ("RATP", "Transports"),
    ("UBER", "Transports"),
    ("BLABLACAR", "Transports"),
    ("PEAGE", "Transports"),
    ("VINCI AUTOROUTE", "Transports"),
    ("PHARMACIE", "Santé"),
    ("DOCTEUR", "Santé"),
    ("CABINET MEDICAL", "Santé"),
    ("LABORATOIRE", "Santé"),
    ("CPAM", "Santé"),
    ("DENTAIRE", "Santé"),
    ("NETFLIX", "Abonnements"),
    ("SPOTIFY", "Abonnements"),
    ("DISNEY", "Abonnements"),
    ("CANAL", "Abonnements"),
    ("AMAZON PRIME", "Abonnements"),
    ("APPLE COM BILL", "Abonnements"),
    ("GOOGLE", "Abonnements"),
    ("AMAZON", "Achats divers"),
    ("FNAC", "Achats divers"),
    ("DECATHLON", "Loisirs"),
    ("CINEMA", "Loisirs"),
    ("RESTAURANT", "Restaurants"),
    ("MCDONALD", "Restaurants"),
    ("BRASSERIE", "Restaurants"),
    ("DGFIP", "Impôts"),
    ("IMPOTS", "Impôts"),
    ("TRESOR PUBLIC", "Impôts"),
    ("MAIF", "Assurances"),
    ("MACIF", "Assurances"),
    ("MAAF", "Assurances"),
    ("AXA", "Assurances"),
    ("ALLIANZ", "Assurances"),
    ("GROUPAMA", "Assurances"),
    ("MATMUT", "Assurances"),
    ("GENERALI", "Assurances"),
    ("MUTUELLE", "Assurances"),
    ("HARMONIE", "Assurances"),
    ("SALAIRE", "Salaire"),
    ("PAIE", "Salaire"),
    ("CAF ", "Prestations"),
    ("POLE EMPLOI", "Prestations"),
    ("FRANCE TRAVAIL", "Prestations"),
]


# --------------------------------------------------------------------------
# Détection des enveloppes manquantes à partir des opérations importées
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleSuggestion:
    """Un commerçant récurrent qu'aucune règle ne reconnaît encore."""

    pattern: str
    sample_label: str
    count: int
    total_cents: int
    envelope_id: int | None
    envelope_name: str

    @property
    def monthly_hint(self) -> int:
        """Ordre de grandeur mensuel, pour situer l'enjeu."""
        return self.total_cents // max(self.count, 1)


# Les motifs livrés (`SEED_RULES`) reconnaissent des enseignes nationales.
# Un relevé réel est surtout fait de commerçants locaux — mais leur nom
# porte presque toujours le métier. Ces mots-clés sont cherchés comme mots
# entiers n'importe où dans le libellé, ce qui rattrape « BOWLING DE VIRE »
# ou « BOULANGERIE MARTIN » qu'aucune liste d'enseignes ne contiendra jamais.
CATEGORY_KEYWORDS: list[tuple[str, str]] = [
    ("BOULANGERIE", "Courses"),
    ("BOULANGER", "Courses"),
    ("PATISSERIE", "Courses"),
    ("BOUCHERIE", "Courses"),
    ("CHARCUTERIE", "Courses"),
    ("POISSONNERIE", "Courses"),
    ("PRIMEUR", "Courses"),
    ("EPICERIE", "Courses"),
    ("FROMAGERIE", "Courses"),
    ("MARCHE", "Courses"),
    ("SUPERMARCHE", "Courses"),
    ("RESTAURANT", "Restaurants"),
    ("BRASSERIE", "Restaurants"),
    ("PIZZERIA", "Restaurants"),
    ("PIZZA", "Restaurants"),
    ("CREPERIE", "Restaurants"),
    ("BISTROT", "Restaurants"),
    ("TRAITEUR", "Restaurants"),
    ("SNACK", "Restaurants"),
    ("KEBAB", "Restaurants"),
    ("SUSHI", "Restaurants"),
    ("BURGER", "Restaurants"),
    ("CAFE", "Restaurants"),
    ("BAR", "Restaurants"),
    ("BOWLING", "Loisirs"),
    ("CINEMA", "Loisirs"),
    ("PISCINE", "Loisirs"),
    ("PARC", "Loisirs"),
    ("MUSEE", "Loisirs"),
    ("THEATRE", "Loisirs"),
    ("CONCERT", "Loisirs"),
    ("SALLE DE SPORT", "Loisirs"),
    ("FITNESS", "Loisirs"),
    ("GOLF", "Loisirs"),
    ("KARTING", "Loisirs"),
    ("LASER", "Loisirs"),
    ("PHARMACIE", "Santé"),
    ("PHARMA", "Santé"),
    ("MEDECIN", "Santé"),
    ("DOCTEUR", "Santé"),
    ("DENTISTE", "Santé"),
    ("DENTAIRE", "Santé"),
    ("KINE", "Santé"),
    ("OPHTALMO", "Santé"),
    ("OPTIQUE", "Santé"),
    ("OPTICIEN", "Santé"),
    ("LABORATOIRE", "Santé"),
    ("RADIOLOGIE", "Santé"),
    ("INFIRMIER", "Santé"),
    ("VETERINAIRE", "Santé"),
    ("GARAGE", "Entretien véhicule"),
    ("CARROSSERIE", "Entretien véhicule"),
    ("PNEU", "Entretien véhicule"),
    ("CONTROLE TECHNIQUE", "Entretien véhicule"),
    ("AUTO ECOLE", "Transports"),
    ("PEAGE", "Transports"),
    ("PARKING", "Transports"),
    ("TAXI", "Transports"),
    ("GARE", "Transports"),
    ("STATION SERVICE", "Carburant"),
    ("CARBURANT", "Carburant"),
    ("COIFFURE", "Achats divers"),
    ("COIFFEUR", "Achats divers"),
    ("INSTITUT", "Achats divers"),
    ("TABAC", "Achats divers"),
    ("PRESSE", "Achats divers"),
    ("LIBRAIRIE", "Achats divers"),
    ("FLEURISTE", "Achats divers"),
    ("JARDINERIE", "Achats divers"),
    ("BRICOLAGE", "Achats divers"),
    ("QUINCAILLERIE", "Achats divers"),
    ("MEUBLE", "Achats divers"),
    ("PRESSING", "Achats divers"),
    ("LAVERIE", "Achats divers"),
    ("CREDIT", "Loyer / Crédit"),
    ("PRET", "Loyer / Crédit"),
    ("SYNDIC", "Charges / Copropriété"),
    ("COPROPRIETE", "Charges / Copropriété"),
    ("CANTINE", "Achats divers"),
    ("PERISCOLAIRE", "Achats divers"),
    ("CRECHE", "Achats divers"),
]


def _envelope_named(db: Session, name: str) -> Envelope | None:
    return db.scalar(select(Envelope).where(Envelope.name == name))


def _seed_guess(db: Session, normalized: str) -> Envelope | None:
    """Enveloppe plausible : enseigne connue d'abord, métier ensuite."""
    for pattern, envelope_name in SEED_RULES:
        if pattern.strip() and pattern.strip() in normalized:
            envelope = _envelope_named(db, envelope_name)
            if envelope is not None:
                return envelope

    for keyword, envelope_name in CATEGORY_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", normalized):
            envelope = _envelope_named(db, envelope_name)
            if envelope is not None:
                return envelope
    return None


def _learned_guess(db: Session, pattern: str) -> Envelope | None:
    """Enveloppe déjà retenue pour un libellé équivalent, classé à la main."""
    rows = db.execute(
        select(Transaction.raw_label, Transaction.label, Transaction.envelope_id)
        .where(Transaction.envelope_id.is_not(None), Transaction.kind != "transfer")
        .limit(2000)
    ).all()
    for raw, label, envelope_id in rows:
        if suggest_pattern(raw or label) == pattern:
            return db.get(Envelope, envelope_id)
    return None


def suggest_rules(
    db: Session, limit: int = 15, min_count: int = 2
) -> list[RuleSuggestion]:
    """Regroupe les dépenses non classées par commerçant et propose une enveloppe.

    C'est le pendant automatique du classement : après un import, plutôt que
    de laisser cinquante lignes orphelines, l'application montre les quelques
    commerçants qui les expliquent et propose une règle pour chacun.
    """
    rows = db.execute(
        select(Transaction.id, Transaction.raw_label, Transaction.label, Transaction.amount_cents)
        .where(
            Transaction.envelope_id.is_(None),
            Transaction.kind != "transfer",
            Transaction.amount_cents < 0,
        )
        .order_by(Transaction.op_date.desc())
    ).all()

    groups: dict[str, dict] = {}
    for _tx_id, raw, label, amount in rows:
        source = raw or label
        pattern = suggest_pattern(source)
        if len(pattern) < 3:
            continue
        bucket = groups.setdefault(
            pattern,
            {
                "count": 0,
                "total": 0,
                "sample": label or source,
                "merchant": merchant_label(source),
            },
        )
        bucket["count"] += 1
        bucket["total"] += -amount

    suggestions: list[RuleSuggestion] = []
    for pattern, bucket in groups.items():
        if bucket["count"] < min_count:
            continue
        guess = _seed_guess(db, bucket["merchant"]) or _learned_guess(db, pattern)
        suggestions.append(
            RuleSuggestion(
                pattern=pattern,
                sample_label=bucket["sample"],
                count=bucket["count"],
                total_cents=bucket["total"],
                envelope_id=guess.id if guess else None,
                envelope_name=guess.name if guess else "",
            )
        )

    suggestions.sort(key=lambda s: (s.total_cents, s.count), reverse=True)
    return suggestions[:limit]


def create_rule(
    db: Session, pattern: str, envelope_id: int, sign: str = "debit"
) -> Rule:
    """Règle issue d'une suggestion acceptée."""
    pattern = pattern.upper().strip()[:200]
    existing = db.scalar(
        select(Rule).where(Rule.pattern == pattern, Rule.matcher == "contains")
    )
    if existing is not None:
        existing.envelope_id = envelope_id
        existing.enabled = True
        db.commit()
        return existing

    rule = Rule(
        envelope_id=envelope_id,
        matcher="contains",
        pattern=pattern,
        sign=sign if sign in {"any", "debit", "credit"} else "debit",
        priority=150,
        auto_learned=True,
    )
    db.add(rule)
    db.commit()
    return rule


def recategorize_all(db: Session) -> int:
    """Repasse toutes les opérations non classées dans les règles courantes."""
    pending = list(
        db.scalars(
            select(Transaction).where(
                Transaction.envelope_id.is_(None), Transaction.kind != "transfer"
            )
        )
    )
    return apply_rules(db, pending)
