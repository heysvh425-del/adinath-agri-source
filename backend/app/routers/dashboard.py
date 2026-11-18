from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from .. import models, schemas, auth
from ..database import get_db

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/summary", response_model=schemas.DashboardSummary)
def summary(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    today_start = datetime.now(UTC).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)

    todays_sales, bills_today = db.query(
        func.coalesce(func.sum(models.Invoice.grand_total), 0),
        func.count(models.Invoice.id),
    ).filter(models.Invoice.date >= today_start).one()

    outstanding_total = db.query(func.sum(models.Invoice.balance_due)).scalar() or 0

    low_stock_count = db.query(func.count(models.Product.id)).filter(
        models.Product.is_active == True,  # noqa: E712
        models.Product.stock <= models.Product.min_stock,
    ).scalar() or 0

    return schemas.DashboardSummary(
        todays_sales=todays_sales,
        bills_today=bills_today,
        outstanding_total=outstanding_total,
        low_stock_count=low_stock_count,
    )


@router.get("/recent-transactions", response_model=List[schemas.InvoiceListOut])
def recent_transactions(
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    invoices = (
        db.query(models.Invoice)
        .options(joinedload(models.Invoice.farmer))
        .order_by(models.Invoice.date.desc()).limit(limit).all()
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


@router.get("/sales-chart", response_model=List[schemas.SalesPoint])
def sales_chart(
    range: str = Query("7d", pattern="^(today|7d|30d)$"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    days = 1 if range == "today" else 7 if range == "7d" else 30
    since = datetime.now(UTC).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)

    invoices = db.query(models.Invoice).filter(models.Invoice.date >= since).all()
    if not invoices:
        return []
    buckets = {}
    for i in range_dates(since, days):
        buckets[i.strftime("%Y-%m-%d")] = Decimal("0")
    for inv in invoices:
        key = inv.date.strftime("%Y-%m-%d")
        if key in buckets:
            buckets[key] += inv.grand_total

    return [schemas.SalesPoint(label=k, total=float(round(v, 2))) for k, v in buckets.items()]


def range_dates(start: datetime, days: int):
    for n in range(days):
        yield start + timedelta(days=n)
