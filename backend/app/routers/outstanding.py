from datetime import UTC, datetime, timedelta
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session, joinedload

from .. import models, schemas, auth
from ..database import get_db

router = APIRouter(prefix="/outstanding", tags=["Outstanding"])


@router.get("/summary", response_model=schemas.OutstandingSummary)
def outstanding_summary(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    today = datetime.now(UTC).date()
    today_start = datetime.combine(today, datetime.min.time())
    tomorrow_start = today_start + timedelta(days=1)
    overdue_cutoff = today_start - timedelta(days=7)
    # With no contractual due-date field, these are age buckets: invoices
    # created today and balances on invoices older than seven days.
    total_outstanding, due_today, overdue, customers_with_dues = db.query(
        func.coalesce(func.sum(models.Invoice.balance_due), 0),
        func.coalesce(func.sum(case((and_(models.Invoice.date >= today_start, models.Invoice.date < tomorrow_start), models.Invoice.balance_due), else_=0)), 0),
        func.coalesce(func.sum(case((models.Invoice.date < overdue_cutoff, models.Invoice.balance_due), else_=0)), 0),
        func.count(func.distinct(models.Invoice.farmer_id)),
    ).filter(models.Invoice.balance_due > 0).one()

    return schemas.OutstandingSummary(
        total_outstanding=round(total_outstanding, 2),
        due_today=round(due_today, 2),
        overdue=round(overdue, 2),
        customers_with_dues=customers_with_dues,
    )


@router.get("", response_model=List[schemas.InvoiceListOut])
def outstanding_invoices(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    invoices = (
        db.query(models.Invoice)
        .options(joinedload(models.Invoice.farmer))
        .filter(models.Invoice.balance_due > 0)
        .order_by(models.Invoice.date.desc())
        .all()
    )
    return [
        schemas.InvoiceListOut(
            id=i.id,
            invoice_no=i.invoice_no,
            farmer_id=i.farmer_id,
            farmer_name=i.farmer.name if i.farmer else "Walk-in Customer",
            date=i.date,
            grand_total=i.grand_total,
            amount_paid=i.amount_paid,
            balance_due=i.balance_due,
            payment_method=i.payment_method,
            status=i.status,
        )
        for i in invoices
    ]
