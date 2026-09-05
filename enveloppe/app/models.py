"""Modèle de données.

Tous les montants sont stockés en **centimes entiers** (`*_cents`) : aucune
opération monétaire ne passe par un flottant. Convention de signe sur les
transactions : dépense négative, recette positive.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    """Compte d'accès à l'application (usage mono-foyer)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_login: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Account(Base):
    """Compte bancaire, support d'épargne ou carte à débit différé.

    Une **carte à débit différé** (`kind="credit"`) est un compte à part
    entière : elle porte les achats à leur date réelle, puis un prélèvement
    unique vient les régler sur le compte courant. Trois paramètres décrivent
    ce cycle :

      - `settlement_account_id` : le compte réellement débité ;
      - `cutoff_day` : jour d'arrêté (0 = dernier jour du mois) — au-delà,
        l'achat bascule sur le cycle suivant ;
      - `settlement_day` : jour du prélèvement (0 = dernier jour du mois).
        S'il précède l'arrêté, le prélèvement tombe le mois suivant.
    """

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20), default="checking")
    # checking | savings | cash | credit
    institution: Mapped[str] = mapped_column(String(120), default="")
    iban_last4: Mapped[str] = mapped_column(String(4), default="")
    opening_balance_cents: Mapped[int] = mapped_column(Integer, default=0)
    is_budgeted: Mapped[bool] = mapped_column(Boolean, default=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    settlement_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), default=None
    )
    cutoff_day: Mapped[int] = mapped_column(Integer, default=0)
    settlement_day: Mapped[int] = mapped_column(Integer, default=0)
    # Dernier rapprochement : date et solde constaté sur le relevé. Sert de
    # repère — au-delà, les opérations pointées sont considérées vérifiées.
    reconciled_on: Mapped[date | None] = mapped_column(Date, default=None)
    reconciled_balance_cents: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="account", foreign_keys="Transaction.account_id"
    )
    settlement_account: Mapped["Account | None"] = relationship(
        "Account", remote_side=[id], foreign_keys=[settlement_account_id]
    )

    @property
    def is_deferred_card(self) -> bool:
        return self.kind == "credit"


class EnvelopeGroup(Base):
    """Regroupement d'enveloppes (Logement, Vie courante, Projets…)."""

    __tablename__ = "envelope_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    envelopes: Mapped[list["Envelope"]] = relationship(
        back_populates="group", order_by="Envelope.position"
    )


class Envelope(Base):
    """Enveloppe budgétaire.

    `kind` :
      - `monthly` : dotation reconduite chaque mois (courses, essence…)
      - `sinking` : provision pour une échéance future (assurance annuelle,
        impôts, vacances) — la dotation mensuelle se déduit de la cible
      - `income`  : enveloppe de revenus, alimente le « reste à budgéter »
    """

    __tablename__ = "envelopes"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("envelope_groups.id", ondelete="SET NULL"), default=None
    )
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(12), default="monthly")
    planned_cents: Mapped[int] = mapped_column(Integer, default=0)
    target_cents: Mapped[int] = mapped_column(Integer, default=0)
    target_date: Mapped[date | None] = mapped_column(Date, default=None)
    rollover: Mapped[bool] = mapped_column(Boolean, default=True)
    essential: Mapped[bool] = mapped_column(Boolean, default=False)
    color: Mapped[str] = mapped_column(String(7), default="#6366f1")
    icon: Mapped[str] = mapped_column(String(8), default="•")
    position: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    group: Mapped[EnvelopeGroup | None] = relationship(back_populates="envelopes")
    allocations: Mapped[list["Allocation"]] = relationship(
        back_populates="envelope", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("name", name="uq_envelope_name"),)


class Allocation(Base):
    """Dotation d'une enveloppe pour un mois donné (période `AAAA-MM`)."""

    __tablename__ = "allocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    envelope_id: Mapped[int] = mapped_column(
        ForeignKey("envelopes.id", ondelete="CASCADE")
    )
    period: Mapped[str] = mapped_column(String(7))
    allocated_cents: Mapped[int] = mapped_column(Integer, default=0)

    envelope: Mapped[Envelope] = relationship(back_populates="allocations")

    __table_args__ = (
        UniqueConstraint("envelope_id", "period", name="uq_allocation_period"),
        Index("ix_allocation_period", "period"),
    )


class ImportProfile(Base):
    """Recette de lecture d'un export bancaire (une par banque/format)."""

    __tablename__ = "import_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    delimiter: Mapped[str] = mapped_column(String(4), default=";")
    encoding: Mapped[str] = mapped_column(String(24), default="utf-8")
    date_format: Mapped[str] = mapped_column(String(24), default="%d/%m/%Y")
    decimal_sep: Mapped[str] = mapped_column(String(1), default=",")
    thousands_sep: Mapped[str] = mapped_column(String(1), default=" ")
    skip_rows: Mapped[int] = mapped_column(Integer, default=0)
    amount_mode: Mapped[str] = mapped_column(String(16), default="single")
    # single | debit_credit
    col_date: Mapped[str] = mapped_column(String(80), default="")
    col_label: Mapped[str] = mapped_column(String(80), default="")
    col_amount: Mapped[str] = mapped_column(String(80), default="")
    col_debit: Mapped[str] = mapped_column(String(80), default="")
    col_credit: Mapped[str] = mapped_column(String(80), default="")
    col_value_date: Mapped[str] = mapped_column(String(80), default="")
    invert_sign: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ImportBatch(Base):
    """Trace d'un import : ce qui est entré, ce qui a été écarté."""

    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(255))
    source_format: Mapped[str] = mapped_column(String(12), default="csv")
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_profiles.id", ondelete="SET NULL"), default=None
    )
    imported_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)

    account: Mapped[Account] = relationship()


class Transaction(Base):
    """Opération bancaire. Dépense < 0, recette > 0."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    envelope_id: Mapped[int | None] = mapped_column(
        ForeignKey("envelopes.id", ondelete="SET NULL"), default=None
    )
    op_date: Mapped[date] = mapped_column(Date)
    value_date: Mapped[date | None] = mapped_column(Date, default=None)
    amount_cents: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(255))
    raw_label: Mapped[str] = mapped_column(Text, default="")
    normalized_label: Mapped[str] = mapped_column(String(255), default="")
    kind: Mapped[str] = mapped_column(String(12), default="expense")
    # expense | income | transfer
    transfer_group: Mapped[str | None] = mapped_column(String(36), default=None)
    notes: Mapped[str] = mapped_column(Text, default="")
    fitid: Mapped[str | None] = mapped_column(String(64), default=None)
    fingerprint: Mapped[str] = mapped_column(String(64))
    import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_batches.id", ondelete="SET NULL"), default=None
    )
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Pointée : retrouvée sur le relevé de la banque. C'est ce qui distingue
    # « la banque le confirme » de « je l'ai saisi ».
    cleared: Mapped[bool] = mapped_column(Boolean, default=False)
    # Achat de carte différée déjà réglé : pointe le prélèvement qui l'a soldé.
    settlement_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), default=None
    )
    # Prélèvement de règlement : désigne la carte qu'il solde.
    settles_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), default=None
    )
    settles_period: Mapped[str | None] = mapped_column(String(7), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    account: Mapped[Account] = relationship(
        back_populates="transactions", foreign_keys=[account_id]
    )
    envelope: Mapped[Envelope | None] = relationship()
    settles_account: Mapped[Account | None] = relationship(
        "Account", foreign_keys=[settles_account_id]
    )
    settlement: Mapped["Transaction | None"] = relationship(
        "Transaction", remote_side=[id], foreign_keys=[settlement_id]
    )

    __table_args__ = (
        UniqueConstraint("account_id", "fingerprint", name="uq_tx_fingerprint"),
        Index("ix_tx_date", "op_date"),
        Index("ix_tx_envelope_date", "envelope_id", "op_date"),
        Index("ix_tx_settlement", "settlement_id"),
    )

    @property
    def period(self) -> str:
        return self.op_date.strftime("%Y-%m")


class Rule(Base):
    """Règle de catégorisation automatique d'une transaction."""

    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    envelope_id: Mapped[int] = mapped_column(
        ForeignKey("envelopes.id", ondelete="CASCADE")
    )
    matcher: Mapped[str] = mapped_column(String(10), default="contains")
    # contains | prefix | regex
    pattern: Mapped[str] = mapped_column(String(200))
    sign: Mapped[str] = mapped_column(String(8), default="any")  # any | debit | credit
    min_cents: Mapped[int | None] = mapped_column(Integer, default=None)
    max_cents: Mapped[int | None] = mapped_column(Integer, default=None)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    auto_learned: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    envelope: Mapped[Envelope] = relationship()


class InsuranceContract(Base):
    """Contrat d'assurance suivi (échéance, préavis, prime, documents)."""

    __tablename__ = "insurance_contracts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(24), default="autre")
    # auto | habitation | sante | prevoyance | emprunteur | scolaire | animal |
    # mobile | juridique | autre
    insurer: Mapped[str] = mapped_column(String(120), default="")
    policy_number: Mapped[str] = mapped_column(String(80), default="")
    premium_cents: Mapped[int] = mapped_column(Integer, default=0)
    frequency: Mapped[str] = mapped_column(String(12), default="annual")
    # monthly | quarterly | semiannual | annual
    start_date: Mapped[date | None] = mapped_column(Date, default=None)
    renewal_date: Mapped[date | None] = mapped_column(Date, default=None)
    notice_period_days: Mapped[int] = mapped_column(Integer, default=60)
    hamon_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    envelope_id: Mapped[int | None] = mapped_column(
        ForeignKey("envelopes.id", ondelete="SET NULL"), default=None
    )
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), default=None
    )
    status: Mapped[str] = mapped_column(String(16), default="active")
    # active | pending_cancel | ended
    contact: Mapped[str] = mapped_column(String(200), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    envelope: Mapped[Envelope | None] = relationship()
    documents: Mapped[list["Document"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan"
    )


class Document(Base):
    """Pièce jointe d'un contrat, stockée hors base sous un nom généré."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("insurance_contracts.id", ondelete="CASCADE")
    )
    original_name: Mapped[str] = mapped_column(String(255))
    stored_name: Mapped[str] = mapped_column(String(80), unique=True)
    mime: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    doc_type: Mapped[str] = mapped_column(String(24), default="contrat")
    # contrat | avenant | attestation | echeancier | facture | sinistre | autre
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    contract: Mapped[InsuranceContract] = relationship(back_populates="documents")


class SavingsGoal(Base):
    """Objectif d'épargne : fonds de sécurité, projet, ou libre."""

    __tablename__ = "savings_goals"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(12), default="project")
    # security | project | other
    target_cents: Mapped[int] = mapped_column(Integer, default=0)
    target_date: Mapped[date | None] = mapped_column(Date, default=None)
    monthly_plan_cents: Mapped[int] = mapped_column(Integer, default=0)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), default=None
    )
    priority: Mapped[int] = mapped_column(Integer, default=100)
    color: Mapped[str] = mapped_column(String(7), default="#0ea5e9")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    contributions: Mapped[list["SavingsContribution"]] = relationship(
        back_populates="goal", cascade="all, delete-orphan"
    )

    account: Mapped[Account | None] = relationship()


class SavingsContribution(Base):
    __tablename__ = "savings_contributions"

    id: Mapped[int] = mapped_column(primary_key=True)
    goal_id: Mapped[int] = mapped_column(
        ForeignKey("savings_goals.id", ondelete="CASCADE")
    )
    op_date: Mapped[date] = mapped_column(Date)
    amount_cents: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(200), default="")
    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), default=None
    )

    goal: Mapped[SavingsGoal] = relationship(back_populates="contributions")
