"""Contrats d'assurance : suivi, échéances et pièces jointes."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Account, Document, Envelope, InsuranceContract
from ..security import current_user
from ..services import documents as docs
from ..services import insurance as service
from ..services.money import euros_to_cents
from ..templating import csrf_guard, flash, render

router = APIRouter(dependencies=[Depends(current_user)])


def _date_or_none(raw: str):
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


@router.get("/assurances", name="insurance_page")
def insurance_page(request: Request, db: Session = Depends(get_session)):
    return render(
        request,
        "insurance.html",
        active="insurance",
        statuses=service.all_statuses(db),
        totals=service.totals(db),
        categories=service.CATEGORY_LABEL,
        frequencies=service.FREQUENCY_LABEL,
        envelopes=list(
            db.scalars(
                select(Envelope)
                .where(Envelope.archived.is_(False))
                .order_by(Envelope.name)
            )
        ),
        accounts=list(db.scalars(select(Account).order_by(Account.position))),
    )


@router.post("/assurances", name="contract_create", dependencies=[Depends(csrf_guard)])
def contract_create(
    request: Request,
    name: str = Form(...),
    category: str = Form("autre"),
    insurer: str = Form(""),
    policy_number: str = Form(""),
    premium: str = Form("0"),
    frequency: str = Form("annual"),
    start_date: str = Form(""),
    renewal_date: str = Form(""),
    notice_period_days: int = Form(60),
    hamon_eligible: str = Form(""),
    envelope_id: str = Form(""),
    account_id: str = Form(""),
    contact: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_session),
):
    contract = InsuranceContract(
        name=name.strip()[:120],
        category=category if category in service.CATEGORY_LABEL else "autre",
        insurer=insurer.strip()[:120],
        policy_number=policy_number.strip()[:80],
        premium_cents=euros_to_cents(premium),
        frequency=frequency if frequency in service.FREQUENCY_PER_YEAR else "annual",
        start_date=_date_or_none(start_date),
        renewal_date=_date_or_none(renewal_date),
        notice_period_days=max(int(notice_period_days), 0),
        hamon_eligible=bool(hamon_eligible),
        envelope_id=int(envelope_id) if envelope_id else None,
        account_id=int(account_id) if account_id else None,
        contact=contact.strip()[:200],
        notes=notes[:5000],
    )
    db.add(contract)
    db.commit()

    response = RedirectResponse(
        request.url_for("contract_detail", contract_id=contract.id), status_code=303
    )
    flash(response, f"Contrat « {contract.name} » enregistré.")
    return response


@router.get("/assurances/{contract_id}", name="contract_detail")
def contract_detail(request: Request, contract_id: int, db: Session = Depends(get_session)):
    contract = db.get(InsuranceContract, contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="Contrat introuvable.")

    return render(
        request,
        "insurance_detail.html",
        active="insurance",
        contract=contract,
        status=service.status_for(contract),
        categories=service.CATEGORY_LABEL,
        frequencies=service.FREQUENCY_LABEL,
        doc_types=docs.DOC_TYPES,
        human_size=docs.human_size,
        envelopes=list(
            db.scalars(
                select(Envelope).where(Envelope.archived.is_(False)).order_by(Envelope.name)
            )
        ),
        accounts=list(db.scalars(select(Account).order_by(Account.position))),
    )


@router.post("/assurances/{contract_id}", name="contract_update", dependencies=[Depends(csrf_guard)])
def contract_update(
    request: Request,
    contract_id: int,
    name: str = Form(...),
    category: str = Form("autre"),
    insurer: str = Form(""),
    policy_number: str = Form(""),
    premium: str = Form("0"),
    frequency: str = Form("annual"),
    start_date: str = Form(""),
    renewal_date: str = Form(""),
    notice_period_days: int = Form(60),
    hamon_eligible: str = Form(""),
    envelope_id: str = Form(""),
    account_id: str = Form(""),
    status: str = Form("active"),
    contact: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_session),
):
    contract = db.get(InsuranceContract, contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="Contrat introuvable.")

    contract.name = name.strip()[:120]
    contract.category = category if category in service.CATEGORY_LABEL else "autre"
    contract.insurer = insurer.strip()[:120]
    contract.policy_number = policy_number.strip()[:80]
    contract.premium_cents = euros_to_cents(premium)
    contract.frequency = frequency if frequency in service.FREQUENCY_PER_YEAR else "annual"
    contract.start_date = _date_or_none(start_date)
    contract.renewal_date = _date_or_none(renewal_date)
    contract.notice_period_days = max(int(notice_period_days), 0)
    contract.hamon_eligible = bool(hamon_eligible)
    contract.envelope_id = int(envelope_id) if envelope_id else None
    contract.account_id = int(account_id) if account_id else None
    contract.status = status if status in {"active", "pending_cancel", "ended"} else "active"
    contract.contact = contact.strip()[:200]
    contract.notes = notes[:5000]
    db.commit()

    response = RedirectResponse(
        request.url_for("contract_detail", contract_id=contract.id), status_code=303
    )
    flash(response, "Contrat mis à jour.")
    return response


@router.post("/assurances/{contract_id}/supprimer", name="contract_delete", dependencies=[Depends(csrf_guard)])
def contract_delete(request: Request, contract_id: int, db: Session = Depends(get_session)):
    contract = db.get(InsuranceContract, contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="Contrat introuvable.")

    for document in list(contract.documents):
        docs.delete(contract.id, document.stored_name)
    db.delete(contract)
    db.commit()

    response = RedirectResponse(request.url_for("insurance_page"), status_code=303)
    flash(response, "Contrat et pièces jointes supprimés.")
    return response


@router.post("/assurances/{contract_id}/documents", name="document_upload", dependencies=[Depends(csrf_guard)])
async def document_upload(
    request: Request,
    contract_id: int,
    doc_type: str = Form("contrat"),
    upload: UploadFile = File(...),
    db: Session = Depends(get_session),
):
    contract = db.get(InsuranceContract, contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="Contrat introuvable.")

    content = await upload.read()
    response = RedirectResponse(
        request.url_for("contract_detail", contract_id=contract_id), status_code=303
    )
    try:
        meta = docs.store(
            contract_id, upload.filename or "document", content, upload.content_type or ""
        )
    except docs.DocumentError as exc:
        flash(response, str(exc), "error")
        return response

    db.add(
        Document(
            contract_id=contract_id,
            doc_type=doc_type if doc_type in docs.DOC_TYPES else "autre",
            **meta,
        )
    )
    db.commit()
    flash(response, f"« {meta['original_name']} » ajouté au contrat.")
    return response


@router.get("/documents/{document_id}", name="document_download")
def document_download(document_id: int, db: Session = Depends(get_session)):
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document introuvable.")

    try:
        path = docs.path_for(document.contract_id, document.stored_name)
    except docs.DocumentError:
        raise HTTPException(status_code=404, detail="Document introuvable.")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Fichier absent du disque.")

    return FileResponse(
        path,
        media_type=document.mime,
        filename=document.original_name,
        content_disposition_type="attachment",
    )


@router.post("/documents/{document_id}/supprimer", name="document_delete", dependencies=[Depends(csrf_guard)])
def document_delete(request: Request, document_id: int, db: Session = Depends(get_session)):
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document introuvable.")

    contract_id = document.contract_id
    docs.delete(contract_id, document.stored_name)
    db.delete(document)
    db.commit()

    response = RedirectResponse(
        request.url_for("contract_detail", contract_id=contract_id), status_code=303
    )
    flash(response, "Document supprimé.")
    return response
