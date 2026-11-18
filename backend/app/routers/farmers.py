from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from .. import models, schemas, auth
from ..database import get_db

router = APIRouter(prefix="/farmers", tags=["Farmers"])


@router.get("", response_model=List[schemas.FarmerListOut])
def list_farmers(
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    q = (
        db.query(
            models.Farmer,
            func.coalesce(func.sum(models.Invoice.grand_total), 0).label("total_purchases"),
            func.coalesce(func.sum(models.Invoice.balance_due), 0).label("outstanding"),
            func.count(models.Invoice.id).label("bill_count"),
            func.max(models.Invoice.date).label("last_purchase"),
        )
        .outerjoin(models.Invoice, models.Invoice.farmer_id == models.Farmer.id)
        .group_by(models.Farmer.id)
    )
    if search:
        like = f"%{search}%"
        q = q.filter((models.Farmer.name.ilike(like)) | (models.Farmer.mobile.ilike(like)))
    return [
        schemas.FarmerListOut(
            id=farmer.id, name=farmer.name, mobile=farmer.mobile, location=farmer.location,
            created_at=farmer.created_at, total_purchases=total, outstanding=outstanding,
            bill_count=bill_count, last_purchase=last_purchase,
        )
        for farmer, total, outstanding, bill_count, last_purchase in q.order_by(models.Farmer.name).all()
    ]


@router.post("", response_model=schemas.FarmerOut, status_code=201)
def create_farmer(
    payload: schemas.FarmerCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    farmer = models.Farmer(**payload.model_dump())
    db.add(farmer)
    db.commit()
    db.refresh(farmer)
    return farmer


@router.put("/{farmer_id}", response_model=schemas.FarmerOut)
def update_farmer(
    farmer_id: int,
    payload: schemas.FarmerUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    farmer = db.query(models.Farmer).filter(models.Farmer.id == farmer_id).first()
    if not farmer:
        raise HTTPException(status_code=404, detail="Farmer not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(farmer, field, value)
    db.commit()
    db.refresh(farmer)
    return farmer


@router.delete("/{farmer_id}", status_code=204)
def delete_farmer(
    farmer_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_roles("admin")),
):
    farmer = db.query(models.Farmer).filter(models.Farmer.id == farmer_id).first()
    if not farmer:
        raise HTTPException(status_code=404, detail="Customer not found")
    if db.query(models.Invoice.id).filter(models.Invoice.farmer_id == farmer_id).first():
        raise HTTPException(status_code=409, detail="This customer has invoice history and cannot be deleted")
    db.delete(farmer)
    db.commit()
    return None


@router.get("/{farmer_id}", response_model=schemas.FarmerProfile)
def get_farmer_profile(
    farmer_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    farmer = db.query(models.Farmer).filter(models.Farmer.id == farmer_id).first()
    if not farmer:
        raise HTTPException(status_code=404, detail="Farmer not found")
    invoices = db.query(models.Invoice).filter(models.Invoice.farmer_id == farmer_id).all()
    total_purchases = sum(i.grand_total for i in invoices)
    outstanding = sum(i.balance_due for i in invoices)
    return schemas.FarmerProfile(
        id=farmer.id,
        name=farmer.name,
        mobile=farmer.mobile,
        location=farmer.location,
        created_at=farmer.created_at,
        total_purchases=total_purchases,
        outstanding=outstanding,
        bill_count=len(invoices),
    )


@router.get("/{farmer_id}/invoices", response_model=List[schemas.InvoiceListOut])
def get_farmer_invoices(
    farmer_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    invoices = (
        db.query(models.Invoice)
        .options(joinedload(models.Invoice.farmer))
        .filter(models.Invoice.farmer_id == farmer_id)
        .order_by(models.Invoice.date.desc())
        .all()
    )
    return [
        schemas.InvoiceListOut(
            id=i.id,
            invoice_no=i.invoice_no,
            farmer_id=i.farmer_id,
            farmer_name=i.customer_name or (i.farmer.name if i.farmer else None),
            date=i.date,
            grand_total=i.grand_total,
            amount_paid=i.amount_paid,
            balance_due=i.balance_due,
            payment_method=i.payment_method,
            status=i.status,
        )
        for i in invoices
    ]
