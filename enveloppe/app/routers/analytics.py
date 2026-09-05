"""Analyses : tendance, répartition, récurrences, pistes d'économies."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..db import get_session
from ..security import current_user
from ..services import analytics as service
from ..services.budget import current_period, month_summary, shift_period
from ..templating import render

router = APIRouter(dependencies=[Depends(current_user)])


@router.get("/analyses", name="analytics_page")
def analytics_page(
    request: Request,
    period: str | None = None,
    months: int = 12,
    db: Session = Depends(get_session),
):
    period = period or current_period()
    months = max(3, min(months, 36))
    series = service.monthly_series(db, months=months, end=period)

    incomes = [point.income for point in series]
    expenses = [point.expense for point in series]
    average_expense = sum(expenses) // len(expenses) if expenses else 0
    average_income = sum(incomes) // len(incomes) if incomes else 0

    return render(
        request,
        "analytics.html",
        active="analytics",
        period=period,
        prev_period=shift_period(period, -1),
        next_period=shift_period(period, 1),
        months=months,
        series=series,
        max_value=max([*incomes, *expenses, 1]),
        average_income=average_income,
        average_expense=average_expense,
        average_net=average_income - average_expense,
        summary=month_summary(db, period),
        breakdown=service.breakdown(db, period),
        merchants=service.top_merchants(db, period),
        recurring=service.recurring_charges(db),
        report=service.savings_opportunities(db, period),
    )
