"""Comptes bancaires et réglages de l'application."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import engine
from ..schema_check import missing_columns
from ..db import get_session
from ..models import Account, ImportBatch, Transaction, User
from ..security import (
    current_user,
    hash_password,
    password_problems,
    through_ingress,
    verify_password,
)
from ..services import cards as cards_service
from ..services import savings as savings_service
from ..services.budget import account_balances
from ..services.money import euros_to_cents, format_cents
from ..templating import csrf_guard, flash, path_for, render

router = APIRouter(dependencies=[Depends(current_user)])

ACCOUNT_KINDS = {
    "checking": "Compte courant",
    "savings": "Épargne",
    "cash": "Espèces",
    "credit": "Carte à débit différé",
}


def _day(raw: str | int) -> int:
    """Jour du mois saisi : 0 (ou vide) signifie « dernier jour »."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if 0 <= value <= 31 else 0


@router.get("/comptes", name="accounts_page")
def accounts_page(request: Request, db: Session = Depends(get_session)):
    accounts = list(db.scalars(select(Account).order_by(Account.position, Account.name)))
    balances = account_balances(db)
    return render(
        request,
        "accounts.html",
        active="accounts",
        accounts=accounts,
        balances=balances,
        projected=cards_service.projected_balances(db, balances),
        outstanding=cards_service.outstanding_by_card(db),
        kinds=ACCOUNT_KINDS,
        total=sum(balances.values()),
        counts={
            account_id: int(count or 0)
            for account_id, count in db.execute(
                select(Transaction.account_id, func.count(Transaction.id)).group_by(
                    Transaction.account_id
                )
            ).all()
        },
    )


@router.post("/comptes", name="account_create", dependencies=[Depends(csrf_guard)])
def account_create(
    request: Request,
    name: str = Form(...),
    kind: str = Form("checking"),
    institution: str = Form(""),
    iban_last4: str = Form(""),
    opening_balance: str = Form("0"),
    is_budgeted: str = Form("on"),
    settlement_account_id: str = Form(""),
    cutoff_day: str = Form("0"),
    settlement_day: str = Form("0"),
    db: Session = Depends(get_session),
):
    last = db.scalar(select(Account).order_by(Account.position.desc()))
    kind = kind if kind in ACCOUNT_KINDS else "checking"
    db.add(
        Account(
            name=name.strip()[:120],
            kind=kind,
            institution=institution.strip()[:120],
            iban_last4="".join(ch for ch in iban_last4 if ch.isdigit())[-4:],
            opening_balance_cents=euros_to_cents(opening_balance),
            is_budgeted=bool(is_budgeted),
            position=(last.position + 1) if last else 0,
            settlement_account_id=(
                int(settlement_account_id)
                if kind == "credit" and settlement_account_id
                else None
            ),
            cutoff_day=_day(cutoff_day) if kind == "credit" else 0,
            settlement_day=_day(settlement_day) if kind == "credit" else 0,
        )
    )
    db.commit()

    response = RedirectResponse(path_for(request, "accounts_page"), status_code=303)
    flash(response, "Compte ajouté.")
    return response


@router.post("/comptes/{account_id}", name="account_update", dependencies=[Depends(csrf_guard)])
def account_update(
    request: Request,
    account_id: int,
    name: str = Form(...),
    kind: str = Form("checking"),
    institution: str = Form(""),
    iban_last4: str = Form(""),
    opening_balance: str = Form("0"),
    is_budgeted: str = Form(""),
    archived: str = Form(""),
    settlement_account_id: str = Form(""),
    cutoff_day: str = Form("0"),
    settlement_day: str = Form("0"),
    db: Session = Depends(get_session),
):
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Compte introuvable.")

    account.name = name.strip()[:120]
    account.kind = kind if kind in ACCOUNT_KINDS else "checking"
    account.institution = institution.strip()[:120]
    account.iban_last4 = "".join(ch for ch in iban_last4 if ch.isdigit())[-4:]
    account.opening_balance_cents = euros_to_cents(opening_balance)
    account.is_budgeted = bool(is_budgeted)
    account.archived = bool(archived)

    if account.kind == "credit":
        target = int(settlement_account_id) if settlement_account_id else None
        # Une carte ne peut pas se régler elle-même.
        account.settlement_account_id = target if target != account.id else None
        account.cutoff_day = _day(cutoff_day)
        account.settlement_day = _day(settlement_day)
    else:
        account.settlement_account_id = None
        account.cutoff_day = 0
        account.settlement_day = 0

    db.commit()

    response = RedirectResponse(path_for(request, "accounts_page"), status_code=303)
    flash(response, "Compte mis à jour.")
    return response


@router.get("/reglages", name="settings_page")
def settings_page(request: Request, db: Session = Depends(get_session)):
    return render(
        request,
        "settings.html",
        active="settings",
        user=db.scalar(select(User)),
        schema_gaps=missing_columns(engine),
        data_dir=str(settings.data_dir),
        currency=settings.currency,
        max_upload_mb=settings.max_upload_mb,
        security_months=savings_service.security_months(db),
        batches=list(
            db.scalars(select(ImportBatch).order_by(ImportBatch.imported_at.desc()).limit(20))
        ),
        counts={
            "transactions": int(db.scalar(select(func.count(Transaction.id))) or 0),
            "accounts": int(db.scalar(select(func.count(Account.id))) or 0),
        },
    )


@router.post("/reglages/mot-de-passe", name="change_password", dependencies=[Depends(csrf_guard)])
def change_password(
    request: Request,
    current: str = Form(""),
    new_password: str = Form(...),
    confirm: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    response = RedirectResponse(path_for(request, "settings_page"), status_code=303)

    # L'ancien mot de passe n'est exigé que s'il existe ET que la requête
    # n'est pas venue par Home Assistant. Derrière l'ingress, HA a déjà
    # authentifié un administrateur : c'est la voie de récupération quand le
    # mot de passe a été perdu, sans avoir à toucher à la base.
    if (
        user.password_hash
        and not through_ingress(request)
        and not verify_password(current, user.password_hash)
    ):
        flash(response, "Mot de passe actuel incorrect.", "error")
        return response
    if new_password != confirm:
        flash(response, "Les deux saisies diffèrent.", "error")
        return response

    problems = password_problems(new_password)
    if problems:
        flash(response, " ".join(problems), "error")
        return response

    user.password_hash = hash_password(new_password)
    db.commit()
    flash(response, "Mot de passe modifié.")
    return response


@router.get("/export/operations.csv", name="export_transactions")
def export_transactions(db: Session = Depends(get_session)):
    """Export intégral : les données restent les vôtres, quoi qu'il arrive."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        ["date", "compte", "libelle", "montant", "enveloppe", "type", "notes"]
    )
    rows = db.scalars(
        select(Transaction).order_by(Transaction.op_date, Transaction.id)
    )
    for tx in rows:
        writer.writerow(
            [
                tx.op_date.isoformat(),
                tx.account.name if tx.account else "",
                tx.label,
                format_cents(tx.amount_cents, with_symbol=False),
                tx.envelope.name if tx.envelope else "",
                tx.kind,
                tx.notes,
            ]
        )
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="operations.csv"'},
    )


@router.post("/reglages/import/{batch_id}/annuler", name="batch_rollback", dependencies=[Depends(csrf_guard)])
def batch_rollback(
    request: Request,
    batch_id: int,
    confirm: str = Form(""),
    db: Session = Depends(get_session),
):
    """Annule un import : supprime les opérations qu'il a créées, et lui seul."""
    response = RedirectResponse(path_for(request, "settings_page"), status_code=303)
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        flash(response, "Import introuvable.", "error")
        return response
    if confirm.strip().upper() != "SUPPRIMER":
        flash(response, "Annulation non confirmée : saisissez SUPPRIMER.", "error")
        return response

    removed = 0
    for tx in db.scalars(select(Transaction).where(Transaction.import_batch_id == batch_id)):
        db.delete(tx)
        removed += 1
    db.delete(batch)
    db.commit()

    flash(response, f"Import annulé : {removed} opération(s) supprimée(s).")
    return response
