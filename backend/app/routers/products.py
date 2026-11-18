from typing import Optional, List
from decimal import Decimal, ROUND_HALF_UP
import logging
import csv
import io
import json
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import models, schemas, auth
from ..database import get_db
from ..database.catalog_metadata import DEFAULT_SOURCE_CATEGORY, SOURCE_BRANDS, SOURCE_CATEGORIES

router = APIRouter(prefix="/products", tags=["Products"])
logger = logging.getLogger(__name__)


def _sync_purchase_cost(data):
    """Derive ex-tax cost and GST component from the tax-inclusive cost."""
    inclusive = data.get("purchase_price_including_gst")
    gst = data.get("gst_rate")
    if inclusive is not None and gst is not None:
        inclusive, gst = Decimal(inclusive), Decimal(gst)
        excluding = (inclusive / (Decimal(1) + gst / Decimal(100))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        data["purchase_price_excluding_gst"] = excluding
        data["purchase_gst_component"] = inclusive - excluding


def _gst_selection(rate):
    if rate is None:
        return "preset"
    return "preset" if Decimal(rate) in {Decimal("0"), Decimal("5"), Decimal("12"), Decimal("18"), Decimal("28")} else "custom"


@router.get("", response_model=List[schemas.ProductOut])
def list_products(
    search: Optional[str] = None,
    category: Optional[str] = Query(None, description="Filter by category, or 'All'"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    q = db.query(models.Product).filter(models.Product.is_active == True)  # noqa: E712
    if category and category != "All":
        q = q.filter(models.Product.category == category)
    if search:
        like = f"%{search}%"
        q = q.filter(
            (models.Product.name.ilike(like)) | (models.Product.sku.ilike(like))
            | (models.Product.brand.ilike(like))
        )
    return q.order_by(models.Product.name).all()


@router.get("/internal", response_model=List[schemas.ProductInternalOut])
def list_internal_products(
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_roles("admin", "accountant")),
):
    """Privileged product view for cost maintenance and accounting workflows."""
    q = db.query(models.Product).filter(models.Product.is_active == True)  # noqa: E712
    if search:
        like = f"%{search}%"
        q = q.filter((models.Product.name.ilike(like)) | (models.Product.sku.ilike(like)) | (models.Product.brand.ilike(like)))
    return q.order_by(models.Product.name).all()


@router.get("/low-stock", response_model=List[schemas.ProductOut])
def low_stock_products(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    products = db.query(models.Product).filter(models.Product.is_active == True).all()  # noqa: E712
    return [p for p in products if p.stock <= p.min_stock]


@router.get("/export.csv")
def export_products_csv(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_roles("admin", "accountant")),
):
    """Export the master including restricted buying prices for authorized staff."""
    fields = ["id", "catalog_serial", "name", "description", "brand", "sku", "category", "unit", "pack_size", "hsn_code", "gst_rate",
              "purchase_price_including_gst", "selling_price", "initial_quantity", "stock", "min_stock",
              "is_active", "source_reference"]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for product in db.query(models.Product).order_by(models.Product.sku).all():
        writer.writerow({field: getattr(product, field, None) for field in fields})
    return Response(buffer.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=adinath-products.csv"})


@router.get("/purchases", response_model=List[schemas.PurchaseEntryOut])
def list_purchases(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_roles("admin", "accountant")),
):
    return db.query(models.PurchaseEntry).order_by(models.PurchaseEntry.purchase_date.desc(), models.PurchaseEntry.id.desc()).all()



@router.get("/{product_id}", response_model=schemas.ProductOut)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.post("/import")
def import_products(
    payload: schemas.ProductImportRequest,
    dry_run: bool = Query(True),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_roles("admin")),
):
    """Preview or apply a SKU-matched import without overwriting existing stock."""
    report = []
    seen_skus = set()
    pending_names = set()
    create_rows = []
    update_rows = []
    for item in payload.products:
        data = item.model_dump()
        sku = data["sku"].strip()
        data["sku"] = sku
        if sku in seen_skus:
            report.append({"sku": sku, "status": "duplicate_in_file", "message": "SKU appears more than once in this file"})
            continue
        seen_skus.add(sku)
        matched = db.query(models.Product).filter(models.Product.sku == sku).first()
        normalized = re.sub(r"\W+", " ", f"{data['name']} {data.get('brand') or ''} {data.get('pack_size') or ''}").strip().casefold()
        if matched:
            update_rows.append((matched, data))
            report.append({"sku": sku, "product_id": matched.id, "status": "matched", "message": "Existing SKU will be updated; current stock is retained"})
            continue
        collision = db.query(models.Product).filter(models.Product.is_active == True).all()  # noqa: E712
        conflict = next((p for p in collision if re.sub(r"\W+", " ", f"{p.name} {p.brand or ''} {p.pack_size or ''}").strip().casefold() == normalized), None)
        if conflict or normalized in pending_names:
            report.append({"sku": sku, "status": "ambiguous_duplicate", "message": f"Similar product exists under another SKU{f' ({conflict.sku})' if conflict else ' in this file'}"})
            continue
        pending_names.add(normalized)
        create_rows.append(data)
        report.append({"sku": sku, "status": "new", "message": "Will be added"})

    if not dry_run:
        if any(row["status"] in {"duplicate_in_file", "ambiguous_duplicate"} for row in report):
            raise HTTPException(status_code=409, detail={"message": "Resolve duplicate or ambiguous rows before importing", "rows": report})
        try:
            for product, data in update_rows:
                data.pop("stock", None)  # sales/purchases remain authoritative for existing quantities
                data.pop("initial_quantity", None)
                data["selling_price"] = data["rate"] = data.get("selling_price", data.get("rate"))
                data["gst_selection_type"] = data.get("gst_selection_type") or _gst_selection(data.get("gst_rate"))
                _sync_purchase_cost(data)
                old_buy = product.purchase_price_including_gst
                old_sale = product.selling_price if product.selling_price is not None else product.rate
                for field, value in data.items():
                    if hasattr(product, field):
                        setattr(product, field, value)
                new_sale = product.selling_price if product.selling_price is not None else product.rate
                if old_buy != product.purchase_price_including_gst or old_sale != new_sale:
                    db.add(models.ProductPriceHistory(product_id=product.id,
                        old_purchase_price=old_buy, new_purchase_price=product.purchase_price_including_gst,
                        old_selling_price=old_sale, new_selling_price=new_sale, changed_by=current_user.id,
                        reason="Product CSV import"))
            for data in create_rows:
                data["selling_price"] = data["rate"] = data.get("selling_price", data.get("rate"))
                data["gst_selection_type"] = data.get("gst_selection_type") or _gst_selection(data.get("gst_rate"))
                _sync_purchase_cost(data)
                product = models.Product(**data)
                db.add(product)
                db.flush()
                if Decimal(str(product.stock or 0)) > 0:
                    db.add(models.StockMovement(product_id=product.id, movement_type="OPENING_STOCK",
                        quantity_in=product.stock, quantity_out=0, previous_stock=0, new_stock=product.stock,
                        reference_number=product.source_reference or "CSV Import", notes="Opening quantity imported",
                        created_by=current_user.id))
            db.add(models.ImportBatch(source_reference=f"Product CSV import {uuid4().hex}",
                inserted_count=len(create_rows), updated_count=len(update_rows),
                report_json=json.dumps(report)))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Product CSV import failed")
            raise
    return {"dry_run": dry_run, "processed": len(payload.products), "created": len(create_rows) if not dry_run else 0,
            "matched": len(update_rows), "rows": report}


@router.get("/{product_id}/stock-movements", response_model=List[schemas.StockMovementOut])
def list_stock_movements(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    if not db.query(models.Product.id).filter(models.Product.id == product_id).first():
        raise HTTPException(status_code=404, detail="Product not found")
    return db.query(models.StockMovement).filter(models.StockMovement.product_id == product_id).order_by(models.StockMovement.movement_date.desc(), models.StockMovement.id.desc()).all()


@router.post("/{product_id}/stock-movements", response_model=schemas.StockMovementOut, status_code=201)
def create_stock_movement(
    product_id: int,
    payload: schemas.StockAdjustmentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_roles("admin")),
):
    product = db.query(models.Product).filter(models.Product.id == product_id, models.Product.is_active == True).first()  # noqa: E712
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    quantity = Decimal(payload.quantity)
    previous = Decimal(product.stock or 0)
    is_in = payload.movement_type in {"PURCHASE", "ADJUSTMENT_IN"}
    if not is_in and quantity > previous:
        raise HTTPException(status_code=400, detail=f"Cannot remove {quantity}; only {previous} {product.unit} available")
    new_stock = previous + quantity if is_in else previous - quantity
    try:
        movement = models.StockMovement(product_id=product.id, movement_type=payload.movement_type,
            quantity_in=quantity if is_in else 0, quantity_out=0 if is_in else quantity,
            previous_stock=previous, new_stock=new_stock, reference_number=payload.reference_number,
            movement_date=payload.purchase_date if payload.movement_type == "PURCHASE" and payload.purchase_date else models.utcnow(),
            notes=payload.notes, created_by=current_user.id, unit_purchase_price=payload.unit_purchase_price)
        product.stock = new_stock
        db.add(movement)
        if payload.movement_type == "PURCHASE":
            if payload.unit_purchase_price is None:
                raise HTTPException(status_code=422, detail="A purchase requires a unit purchase price")
            total = (quantity * Decimal(payload.unit_purchase_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            entry = models.PurchaseEntry(product_id=product.id, supplier=payload.supplier or product.supplier,
                invoice_number=payload.reference_number, purchase_date=movement.movement_date,
                product_name=product.name, hsn_code=product.hsn_code,
                master_buying_price_snapshot=product.purchase_price_including_gst,
                master_selling_price_snapshot=product.selling_price if product.selling_price is not None else product.rate,
                quantity=quantity, unit_price_including_gst=payload.unit_purchase_price,
                gst_rate=product.gst_rate, total_amount=total, notes=payload.notes, created_by=current_user.id)
            db.add(entry)
        db.commit()
        db.refresh(movement)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Failed to record stock movement product_id=%s", product_id)
        raise
    return movement


@router.post("", response_model=schemas.ProductOut, status_code=201)
def create_product(
    payload: schemas.ProductCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_roles("admin")),
):
    if db.query(models.Product).filter(models.Product.sku == payload.sku).first():
        raise HTTPException(status_code=400, detail="SKU already exists")
    data = payload.model_dump()
    data["gst_selection_type"] = data.get("gst_selection_type") or _gst_selection(data.get("gst_rate"))
    selling_price = data.get("selling_price", data.get("rate"))
    data["selling_price"] = data["rate"] = selling_price
    _sync_purchase_cost(data)
    product = models.Product(**data)
    db.add(product)
    try:
        db.commit()
        db.refresh(product)
    except Exception:
        db.rollback()
        logger.exception("Failed to create product sku=%s", payload.sku)
        raise
    return product


@router.put("/{product_id}", response_model=schemas.ProductOut)
def update_product(
    product_id: int,
    payload: schemas.ProductUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_roles("admin")),
):
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if payload.sku is not None and payload.sku != product.sku:
        duplicate = db.query(models.Product).filter(models.Product.sku == payload.sku).first()
        if duplicate:
            raise HTTPException(status_code=400, detail="SKU already exists")
    changes = payload.model_dump(exclude_unset=True)
    if "gst_rate" in changes and "gst_selection_type" not in changes:
        changes["gst_selection_type"] = _gst_selection(changes["gst_rate"])
    if "selling_price" in changes or "rate" in changes:
        selling_price = changes.get("selling_price", changes.get("rate"))
        changes["selling_price"] = changes["rate"] = selling_price
    calculation = {
        "purchase_price_including_gst": changes.get("purchase_price_including_gst", product.purchase_price_including_gst),
        "gst_rate": changes.get("gst_rate", product.gst_rate),
    }
    if "purchase_price_including_gst" in changes or "gst_rate" in changes:
        _sync_purchase_cost(calculation)
        changes.update({k: calculation[k] for k in ("purchase_price_excluding_gst", "purchase_gst_component") if k in calculation})
    old_purchase = product.purchase_price_including_gst
    old_selling = product.selling_price if product.selling_price is not None else product.rate
    for field, value in changes.items():
        setattr(product, field, value)
    new_purchase = product.purchase_price_including_gst
    new_selling = product.selling_price if product.selling_price is not None else product.rate
    if old_purchase != new_purchase or old_selling != new_selling:
        db.add(models.ProductPriceHistory(
            product_id=product.id,
            old_purchase_price=old_purchase,
            new_purchase_price=new_purchase,
            old_selling_price=old_selling,
            new_selling_price=new_selling,
            changed_by=current_user.id,
            reason="Product pricing edited",
        ))
    try:
        db.commit()
        db.refresh(product)
    except Exception:
        db.rollback()
        logger.exception("Failed to update product id=%s sku=%s", product.id, product.sku)
        raise
    return product


@router.delete("/{product_id}", status_code=204)
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_roles("admin")),
):
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    # Soft delete to preserve invoice history integrity
    product.is_active = False
    db.commit()
    return None
