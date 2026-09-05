"""Import de relevés : dépôt du fichier, contrôle du mapping, insertion."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_session
from ..models import Account, ImportBatch, ImportProfile, Transaction
from ..security import current_user
from ..services import cards as cards_service
from ..services import importer
from ..services import pdf_import
from ..templating import csrf_guard, flash, render

router = APIRouter(dependencies=[Depends(current_user)])

MAX_IMPORT_BYTES = 20 * 1024 * 1024
STAGING_TTL_SECONDS = 6 * 3600


def _purge_staging() -> None:
    """Les fichiers déposés ne survivent pas à la session d'import."""
    now = time.time()
    for path in settings.imports_dir.glob("*"):
        try:
            if now - path.stat().st_mtime > STAGING_TTL_SECONDS:
                path.unlink()
        except OSError:
            continue


def _staging_path(token: str) -> Path:
    """Chemin du fichier déposé, vérifié contre toute traversée de répertoire."""
    base = settings.imports_dir.resolve()
    candidate = (base / Path(token).name).resolve()
    if not str(candidate).startswith(str(base)) or not candidate.exists():
        raise HTTPException(status_code=404, detail="Fichier d'import expiré ou introuvable.")
    return candidate


@router.get("/import", name="import_form")
def import_form(request: Request, db: Session = Depends(get_session)):
    _purge_staging()
    return render(
        request,
        "import.html",
        active="import",
        accounts=list(
            db.scalars(
                select(Account).where(Account.archived.is_(False)).order_by(Account.position)
            )
        ),
        profiles=list(db.scalars(select(ImportProfile).order_by(ImportProfile.name))),
        batches=list(
            db.scalars(select(ImportBatch).order_by(ImportBatch.imported_at.desc()).limit(10))
        ),
    )


@router.post("/import/analyse", name="import_preview", dependencies=[Depends(csrf_guard)])
async def import_preview(
    request: Request,
    account_id: int = Form(...),
    profile_id: str = Form(""),
    upload: UploadFile = File(...),
    db: Session = Depends(get_session),
):
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Compte introuvable.")

    content = await upload.read()
    if not content:
        response = RedirectResponse(request.url_for("import_form"), status_code=303)
        flash(response, "Fichier vide.", "error")
        return response
    if len(content) > MAX_IMPORT_BYTES:
        response = RedirectResponse(request.url_for("import_form"), status_code=303)
        flash(response, "Fichier trop volumineux (20 Mo maximum).", "error")
        return response

    suffix = Path(upload.filename or "releve.csv").suffix.lower()
    if suffix not in {".csv", ".txt", ".ofx", ".qfx", ".pdf"}:
        response = RedirectResponse(request.url_for("import_form"), status_code=303)
        flash(response, "Formats acceptés : CSV, TXT, OFX, QFX, PDF.", "error")
        return response

    token = f"{uuid.uuid4().hex}{suffix}"
    staged = settings.imports_dir / token
    staged.write_bytes(content)
    staged.chmod(0o600)

    if suffix == ".pdf":
        try:
            pdf_preview = pdf_import.preview_pdf(content)
        except ValueError as exc:
            staged.unlink(missing_ok=True)
            response = RedirectResponse(request.url_for("import_form"), status_code=303)
            flash(response, str(exc), "error")
            return response

        return render(
            request,
            "import_confirm.html",
            active="import",
            mode="pdf",
            account=account,
            token=token,
            filename=upload.filename,
            pdf=pdf_preview,
            card_accounts=list(
                db.scalars(
                    select(Account)
                    .where(Account.kind == "credit", Account.archived.is_(False))
                    .order_by(Account.name)
                )
            ),
            parsed=pdf_preview.parsed[:20],
            parsed_count=len(pdf_preview.parsed),
            total_rows=pdf_preview.lines_read,
            errors=pdf_preview.warnings[:10],
            preview=None,
            profiles=[],
            profile=None,
        )

    if suffix in {".ofx", ".qfx"}:
        parsed, total, errors = importer.parse_ofx(content)
        return render(
            request,
            "import_confirm.html",
            active="import",
            mode="ofx",
            account=account,
            token=token,
            filename=upload.filename,
            parsed=parsed[:15],
            parsed_count=len(parsed),
            total_rows=total,
            errors=errors[:10],
            preview=None,
            profiles=[],
            profile=None,
            pdf=None,
        )

    try:
        preview = importer.preview_csv(content)
    except ValueError as exc:
        staged.unlink(missing_ok=True)
        response = RedirectResponse(request.url_for("import_form"), status_code=303)
        flash(response, str(exc), "error")
        return response

    profile = db.get(ImportProfile, int(profile_id)) if profile_id else None
    if profile is None:
        profile = importer.default_profile("", preview)

    return render(
        request,
        "import_confirm.html",
        active="import",
        mode="csv",
        account=account,
        token=token,
        filename=upload.filename,
        preview=preview,
        profile=profile,
        profiles=list(db.scalars(select(ImportProfile).order_by(ImportProfile.name))),
        parsed=[],
        parsed_count=0,
        total_rows=len(preview.sample),
        errors=[],
        pdf=None,
    )


@router.post("/import/valider", name="import_confirm", dependencies=[Depends(csrf_guard)])
async def import_confirm(
    request: Request,
    account_id: int = Form(...),
    token: str = Form(...),
    filename: str = Form("releve"),
    mode: str = Form("csv"),
    save_profile_as: str = Form(""),
    delimiter: str = Form(";"),
    encoding: str = Form("utf-8"),
    date_format: str = Form("%d/%m/%Y"),
    decimal_sep: str = Form(","),
    skip_rows: int = Form(0),
    amount_mode: str = Form("single"),
    col_date: str = Form(""),
    col_label: str = Form(""),
    col_amount: str = Form(""),
    col_debit: str = Form(""),
    col_credit: str = Form(""),
    col_value_date: str = Form(""),
    invert_sign: str = Form(""),
    unsigned_as_debit: str = Form("on"),
    allow_near_duplicates: str = Form(""),
    db: Session = Depends(get_session),
):
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Compte introuvable.")

    staged = _staging_path(token)
    content = staged.read_bytes()

    if mode == "ofx":
        parsed, total, errors = importer.parse_ofx(content)
        profile_id = None
    elif mode == "pdf":
        pdf_preview = pdf_import.preview_pdf(
            content, unsigned_as_debit=bool(unsigned_as_debit)
        )
        parsed = pdf_preview.parsed
        total = pdf_preview.lines_read
        errors = pdf_preview.warnings
        profile_id = None
    else:
        profile = ImportProfile(
            name=save_profile_as.strip()[:80] or f"tmp-{uuid.uuid4().hex[:6]}",
            delimiter="\t" if delimiter == "\\t" else delimiter,
            encoding=encoding,
            date_format=date_format,
            decimal_sep=decimal_sep,
            thousands_sep=" " if decimal_sep == "," else ",",
            skip_rows=skip_rows,
            amount_mode=amount_mode,
            col_date=col_date,
            col_label=col_label,
            col_amount=col_amount,
            col_debit=col_debit,
            col_credit=col_credit,
            col_value_date=col_value_date,
            invert_sign=bool(invert_sign),
        )
        parsed, total, errors = importer.parse_csv(content, profile)

        profile_id = None
        if save_profile_as.strip():
            existing = db.scalar(
                select(ImportProfile).where(ImportProfile.name == profile.name)
            )
            if existing is None:
                db.add(profile)
                db.commit()
                profile_id = profile.id
            else:
                profile_id = existing.id

    if not parsed:
        response = RedirectResponse(request.url_for("import_form"), status_code=303)
        flash(
            response,
            "Aucune opération lisible : " + (errors[0] if errors else "vérifiez le mapping."),
            "error",
        )
        return response

    result = importer.ingest(
        db,
        account=account,
        parsed=parsed,
        filename=filename,
        source_format=mode,
        total_rows=total,
        errors=errors,
        profile_id=profile_id,
        strict_duplicates=not allow_near_duplicates,
    )
    card_reports = []
    if mode == "pdf" and pdf_preview.card_blocks:
        form = await request.form()
        card_reports = _import_card_blocks(
            db, account, pdf_preview, form, filename
        )

    staged.unlink(missing_ok=True)

    return render(
        request,
        "import_result.html",
        active="import",
        account=account,
        result=result,
        filename=filename,
        card_reports=card_reports,
    )


def _import_card_blocks(db, bank_account, preview, form, filename: str) -> list[dict]:
    """Range chaque bloc de carte différée dans son compte, puis le rapproche.

    Le relevé nomme lui-même le lien : ces achats-là, ce prélèvement-là. On
    n'a donc rien à deviner — les achats gardent leur date réelle et le
    prélèvement devient neutre pour le budget.
    """
    rapports = []

    for index, block in enumerate(preview.card_blocks):
        choix = str(form.get(f"card_target_{index}", "")).strip()
        if not choix:
            rapports.append({"block": block, "skipped": True})
            continue

        if choix == "new":
            card = Account(
                name=f"Carte n° {block.card_number}"
                + (f" — {block.holder.title()}" if block.holder else ""),
                kind="credit",
                institution=bank_account.institution,
                settlement_account_id=bank_account.id,
                settlement_day=block.settlement_on.day,
                cutoff_day=0,
                position=(db.scalar(select(func.max(Account.position))) or 0) + 1,
            )
            db.add(card)
            db.commit()
        else:
            card = db.get(Account, int(choix))
            if card is None or card.kind != "credit":
                rapports.append({"block": block, "skipped": True})
                continue
            if card.settlement_account_id is None:
                card.settlement_account_id = bank_account.id
                card.settlement_day = block.settlement_on.day
                db.commit()

        achats = importer.ingest(
            db,
            account=card,
            parsed=block.purchases,
            filename=f"{filename} — carte {block.card_number}",
            source_format="pdf",
            total_rows=len(block.purchases),
            errors=[],
        )

        settlement = db.scalar(
            select(Transaction).where(
                Transaction.account_id == bank_account.id,
                Transaction.op_date == block.settlement_on,
                Transaction.amount_cents == -block.announced_cents,
                Transaction.raw_label.like(f"%{block.card_number}%"),
            )
        )

        attached = 0
        if settlement is not None and block.purchases:
            dates = [item.op_date for item in block.purchases]
            portefeuille = list(
                db.scalars(
                    select(Transaction).where(
                        Transaction.account_id == card.id,
                        Transaction.settlement_id.is_(None),
                        Transaction.op_date >= min(dates),
                        Transaction.op_date <= max(dates),
                    )
                )
            )
            attached = cards_service.attach_settlement(
                db, card, settlement, portefeuille
            )

        rapports.append(
            {
                "block": block,
                "card": card,
                "result": achats,
                "attached": attached,
                "settled": settlement is not None,
                "skipped": False,
            }
        )

    return rapports


@router.post("/import/profils/{profile_id}/supprimer", name="profile_delete", dependencies=[Depends(csrf_guard)])
def profile_delete(request: Request, profile_id: int, db: Session = Depends(get_session)):
    profile = db.get(ImportProfile, profile_id)
    if profile is not None:
        db.delete(profile)
        db.commit()
    response = RedirectResponse(request.url_for("import_form"), status_code=303)
    flash(response, "Profil supprimé.")
    return response
