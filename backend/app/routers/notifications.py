from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import auth, models, schemas
from ..database import get_db

router = APIRouter(prefix="/notifications", tags=["Notifications"])


def now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@router.get("", response_model=List[schemas.NotificationOut])
def list_notifications(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    now = now_utc()
    notifications = []
    products = db.query(models.Product).filter(models.Product.is_active == True).all()  # noqa: E712
    low_stock = [p for p in products if p.stock <= p.min_stock]
    if low_stock:
        names = ", ".join(p.name for p in low_stock[:2])
        extra = f" and {len(low_stock) - 2} more" if len(low_stock) > 2 else ""
        notifications.append(schemas.NotificationOut(
            id="low-stock", type="low_stock", severity="warning", title="Low stock needs attention",
            message=f"{len(low_stock)} item(s) are at or below their stock threshold: {names}{extra}.",
            created_at=now, action_view="inventory",
        ))

    expiring = [p for p in products if p.expires_on and p.expires_on <= now + timedelta(days=30)]
    if expiring:
        expired = sum(1 for p in expiring if p.expires_on <= now)
        severity = "critical" if expired else "warning"
        label = "expired" if expired else "expiring within 30 days"
        notifications.append(schemas.NotificationOut(
            id="product-expiry", type="expiry", severity=severity, title="Product expiry alert",
            message=f"{len(expiring)} product(s) are {label}. Review their inventory before selling.",
            created_at=min(p.expires_on for p in expiring), action_view="products",
        ))

    overdue_cutoff = now - timedelta(days=7)
    due_invoices = db.query(models.Invoice).filter(models.Invoice.balance_due > 0).all()
    overdue = [i for i in due_invoices if i.date < overdue_cutoff]
    outstanding = sum((Decimal(i.balance_due) for i in due_invoices), Decimal("0"))
    if due_invoices:
        notifications.append(schemas.NotificationOut(
            id="outstanding-payments", type="payment", severity="warning" if overdue else "info",
            title="Outstanding payments",
            message=f"₹{outstanding:,.2f} remains across {len(due_invoices)} invoice(s)" + (f"; {len(overdue)} are over 7 days old." if overdue else "."),
            created_at=max(i.date for i in due_invoices), action_view="outstanding",
        ))

    recent_sales = db.query(models.Invoice).filter(models.Invoice.date >= now - timedelta(hours=24)).order_by(models.Invoice.date.desc()).limit(5).all()
    if recent_sales:
        latest = recent_sales[0]
        notifications.append(schemas.NotificationOut(
            id=f"sale-{latest.id}", type="sale", severity="success", title="New sale recorded",
            message=f"{latest.invoice_no} for ₹{Decimal(latest.grand_total):,.2f} was created in the last 24 hours.",
            created_at=latest.date, action_view="sales",
        ))

    notifications.append(schemas.NotificationOut(
        id="system-status", type="system", severity="info", title="System check complete",
        message="Billing, inventory, and customer records are available.", created_at=now, action_view="dashboard",
    ))
    return sorted(notifications, key=lambda item: item.created_at, reverse=True)
