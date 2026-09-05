"""Épargne : objectifs, versements et fonds de sécurité."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Account, SavingsContribution, SavingsGoal
from ..security import current_user
from ..services import savings as service
from ..services.money import euros_to_cents
from ..templating import csrf_guard, flash, path_for, render

router = APIRouter(dependencies=[Depends(current_user)])


@router.get("/epargne", name="savings_page")
def savings_page(request: Request, db: Session = Depends(get_session)):
    progress = service.all_progress(db)
    return render(
        request,
        "savings.html",
        active="savings",
        progress=progress,
        security=service.security_fund(db),
        savings_hints=service.suggest_savings_accounts(db),
        savings_accounts=list(
            db.scalars(
                select(Account).where(
                    Account.kind == "savings", Account.archived.is_(False)
                )
            )
        ),
        kinds=service.KIND_LABEL,
        accounts=list(
            db.scalars(
                select(Account).where(Account.archived.is_(False)).order_by(Account.position)
            )
        ),
        total_saved=sum(item.saved_cents for item in progress),
        total_target=sum(item.goal.target_cents for item in progress),
        recent=list(
            db.scalars(
                select(SavingsContribution)
                .order_by(SavingsContribution.op_date.desc())
                .limit(12)
            )
        ),
    )


@router.post("/epargne", name="goal_create", dependencies=[Depends(csrf_guard)])
def goal_create(
    request: Request,
    name: str = Form(...),
    kind: str = Form("project"),
    target: str = Form("0"),
    target_date: str = Form(""),
    monthly_plan: str = Form("0"),
    account_id: str = Form(""),
    priority: int = Form(100),
    color: str = Form("#0ea5e9"),
    notes: str = Form(""),
    db: Session = Depends(get_session),
):
    goal = SavingsGoal(
        name=name.strip()[:120],
        kind=kind if kind in service.KIND_LABEL else "project",
        target_cents=euros_to_cents(target),
        target_date=(
            datetime.strptime(target_date, "%Y-%m-%d").date() if target_date else None
        ),
        monthly_plan_cents=euros_to_cents(monthly_plan),
        account_id=int(account_id) if account_id else None,
        priority=priority,
        color=color[:7],
        notes=notes[:2000],
    )
    db.add(goal)
    db.commit()

    response = RedirectResponse(path_for(request, "savings_page"), status_code=303)
    flash(response, f"Objectif « {goal.name} » créé.")
    return response


@router.post("/epargne/securite", name="security_settings", dependencies=[Depends(csrf_guard)])
def security_settings(
    request: Request, months: int = Form(4), db: Session = Depends(get_session)
):
    service.set_security_months(db, max(1, min(int(months), 24)))
    response = RedirectResponse(path_for(request, "savings_page"), status_code=303)
    flash(response, "Cible du fonds de sécurité mise à jour.")
    return response


@router.post("/epargne/{goal_id}", name="goal_update", dependencies=[Depends(csrf_guard)])
def goal_update(
    request: Request,
    goal_id: int,
    name: str = Form(...),
    kind: str = Form("project"),
    target: str = Form("0"),
    target_date: str = Form(""),
    monthly_plan: str = Form("0"),
    account_id: str = Form(""),
    priority: int = Form(100),
    color: str = Form("#0ea5e9"),
    archived: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_session),
):
    goal = db.get(SavingsGoal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Objectif introuvable.")

    goal.name = name.strip()[:120]
    goal.kind = kind if kind in service.KIND_LABEL else "project"
    goal.target_cents = euros_to_cents(target)
    goal.target_date = (
        datetime.strptime(target_date, "%Y-%m-%d").date() if target_date else None
    )
    goal.monthly_plan_cents = euros_to_cents(monthly_plan)
    goal.account_id = int(account_id) if account_id else None
    goal.priority = priority
    goal.color = color[:7]
    goal.archived = bool(archived)
    goal.notes = notes[:2000]
    db.commit()

    response = RedirectResponse(path_for(request, "savings_page"), status_code=303)
    flash(response, "Objectif mis à jour.")
    return response


@router.post("/epargne/{goal_id}/versement", name="contribution_add", dependencies=[Depends(csrf_guard)])
def contribution_add(
    request: Request,
    goal_id: int,
    amount: str = Form(...),
    op_date: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_session),
):
    goal = db.get(SavingsGoal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Objectif introuvable.")

    when = (
        datetime.strptime(op_date, "%Y-%m-%d").date() if op_date else date.today()
    )
    service.add_contribution(db, goal, euros_to_cents(amount), when, note)

    response = RedirectResponse(path_for(request, "savings_page"), status_code=303)
    flash(response, "Versement enregistré.")
    return response


@router.post("/epargne/versements/{contribution_id}/supprimer", name="contribution_delete", dependencies=[Depends(csrf_guard)])
def contribution_delete(
    request: Request, contribution_id: int, db: Session = Depends(get_session)
):
    contribution = db.get(SavingsContribution, contribution_id)
    if contribution is not None:
        db.delete(contribution)
        db.commit()
    response = RedirectResponse(path_for(request, "savings_page"), status_code=303)
    flash(response, "Versement supprimé.")
    return response


@router.post(
    # Deux segments, volontairement : « /epargne/xxx » est déjà pris par la
    # mise à jour d'un objectif, qui attend un identifiant entier et
    # capturerait ce chemin avant lui.
    "/epargne/comptes/detecte",
    name="savings_account_declare",
    dependencies=[Depends(csrf_guard)],
)
def savings_account_declare(
    request: Request,
    pattern: str = Form(...),
    name: str = Form(...),
    balance: str = Form("0"),
    db: Session = Depends(get_session),
):
    """Déclare une épargne repérée dans les relevés et requalifie le passé."""
    account, requalified = service.declare_savings_account(
        db, pattern, name, euros_to_cents(balance)
    )
    response = RedirectResponse(path_for(request, "savings_page"), status_code=303)
    flash(
        response,
        f"Compte « {account.name} » créé — {requalified} versement(s) requalifié(s) "
        "en virement interne, et retirés de vos dépenses.",
    )
    return response
