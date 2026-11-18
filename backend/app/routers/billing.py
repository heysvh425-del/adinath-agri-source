from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
import hashlib
from typing import List
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import text

from .. import models, schemas, auth
from ..database import get_db

router = APIRouter(prefix="/invoices", tags=["Billing"])

TAX_RATE = Decimal("0.05")  # Compatibility rate for legacy products without GST configured.
MONEY = Decimal("0.01")


def _money(value: Decimal | float) -> Decimal:
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def _invoice_matches_payload(invoice: models.Invoice, payload: schemas.InvoiceCreate) -> bool:
    payload_hash = hashlib.sha256(payload.model_dump_json().encode("utf-8")).hexdigest()
    if invoice.client_payload_hash:
        return invoice.client_payload_hash == payload_hash
    requested_quantities: dict[int, Decimal] = {}
    for item in payload.items:
        requested_quantities[item.product_id] = requested_quantities.get(item.product_id, Decimal("0")) + Decimal(str(item.qty))

    saved_quantities: dict[int, Decimal] = {}
    for item in invoice.items:
        if item.product_id is not None:
            saved_quantities[item.product_id] = saved_quantities.get(item.product_id, Decimal("0")) + Decimal(item.qty)

    product_discount = Decimal(invoice.discount) - Decimal(invoice.invoice_level_discount or 0)
    requested_discount = min(_money(payload.discount), max(Decimal("0"), Decimal(invoice.subtotal) - product_discount))
    requested_paid = min(_money(payload.amount_paid), Decimal(invoice.grand_total))
    return (
        invoice.farmer_id == payload.farmer_id
        and saved_quantities == requested_quantities
        and Decimal(invoice.invoice_level_discount or 0) == requested_discount
        and invoice.payment_method == payload.payment_method
        and Decimal(invoice.amount_paid) == requested_paid
    )


def _number_words(number: int) -> str:
    ones = ("Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
            "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
            "Eighteen", "Nineteen")
    tens = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")
    def under_thousand(n):
        parts = []
        if n >= 100:
            parts.extend((ones[n // 100], "Hundred")); n %= 100
        if n >= 20:
            parts.append(tens[n // 10]); n %= 10
        if n:
            parts.append(ones[n])
        return " ".join(parts)
    if number == 0: return "Zero"
    parts = []
    for size, label in ((10000000, "Crore"), (100000, "Lakh"), (1000, "Thousand"), (1, "")):
        chunk, number = divmod(number, size)
        if chunk:
            parts.append(under_thousand(chunk) + (f" {label}" if label else ""))
    return " ".join(parts)


def amount_in_words(amount: Decimal) -> str:
    paise_total = int((_money(amount) * 100).to_integral_value())
    rupees, paise = divmod(paise_total, 100)
    words = f"Rupees {_number_words(rupees)}"
    if paise: words += f" and Paise {_number_words(paise)}"
    return words + " Only"


@router.post("", response_model=schemas.InvoiceOut, status_code=201)
def create_invoice(
    payload: schemas.InvoiceCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
    client_request_id: str | None = Header(default=None, alias="X-Idempotency-Key"),
):
    if client_request_id and len(client_request_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid checkout request key")
    if client_request_id:
        existing = db.query(models.Invoice).filter(
            models.Invoice.client_request_id == client_request_id
        ).first()
        if existing:
            if not _invoice_matches_payload(existing, payload):
                raise HTTPException(status_code=409, detail="Checkout key was already used for a different bill")
            return existing
        # The read above opens SQLAlchemy's deferred SQLite transaction.
        # End it before requesting an IMMEDIATE write lock below.
        db.rollback()
    # SQLite only permits one writer. Taking the write lock before reading
    # inventory makes validation, invoice numbering and stock deduction one
    # transaction rather than a check-then-write race.
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        # Check again after the write lock. A concurrent retry may have
        # completed while this request waited for SQLite.
        if client_request_id:
            existing = db.query(models.Invoice).filter(
                models.Invoice.client_request_id == client_request_id
            ).first()
            if existing:
                if not _invoice_matches_payload(existing, payload):
                    raise HTTPException(status_code=409, detail="Checkout key was already used for a different bill")
                db.rollback()
                return existing
        return _create_invoice(payload, db, current_user, client_request_id)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def _create_invoice(
    payload: schemas.InvoiceCreate, db: Session, current_user: models.User,
    client_request_id: str | None = None,
) -> models.Invoice:
    if not payload.items:
        raise HTTPException(status_code=400, detail="A bill needs at least one item")
    if payload.payment_method == models.PaymentMethodEnum.credit and payload.amount_paid > 0:
        raise HTTPException(status_code=400, detail="A credit invoice cannot include an immediate payment")

    farmer = None
    if payload.farmer_id is not None:
        farmer = db.query(models.Farmer).filter(models.Farmer.id == payload.farmer_id).first()
        if not farmer:
            raise HTTPException(status_code=404, detail="Farmer not found")

    requested_quantities: dict[int, Decimal] = {}
    for item in payload.items:
        requested_quantities[item.product_id] = requested_quantities.get(item.product_id, Decimal("0")) + Decimal(str(item.qty))

    line_items = []
    subtotal = Decimal("0")

    # Validate aggregated quantities first so duplicate lines cannot bypass stock checks.
    products_by_id = {}
    for product_id, qty in requested_quantities.items():
        product = db.query(models.Product).filter(models.Product.id == product_id).first()
        if not product or not product.is_active:
            raise HTTPException(status_code=404, detail=f"Product {product_id} not found")
        if product.selling_price is None and product.rate is None and not any(
            item.product_id == product_id and item.rate is not None for item in payload.items
        ):
            raise HTTPException(status_code=400, detail=f"Price is not set for '{product.name}'")
        if qty <= 0:
            raise HTTPException(status_code=400, detail="Quantity must be greater than zero")
        if Decimal(product.stock) < qty:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient stock for '{product.name}' (available: {product.stock})",
            )
        products_by_id[product_id] = product

    for requested in payload.items:
        product, qty = products_by_id[requested.product_id], Decimal(requested.qty)
        default_rate = product.selling_price if product.selling_price is not None else product.rate
        unit_rate = Decimal(requested.rate if requested.rate is not None else default_rate)
        rate = Decimal(str(product.gst_rate if product.gst_rate is not None else TAX_RATE * 100))
        tax_fraction = rate / Decimal("100")
        mode = requested.selling_price_tax_mode or product.selling_price_tax_mode or "exclusive"
        listed_amount = _money(unit_rate * qty)
        discount_gross = _money(
            requested.discount_amount + listed_amount * Decimal(requested.discount_percentage) / Decimal("100")
        )
        discount_gross = min(discount_gross, listed_amount)
        taxable_before_discount = (
            _money(listed_amount / (Decimal("1") + tax_fraction))
            if mode == "inclusive" and rate > 0 else listed_amount
        )
        product_discount = (
            _money(discount_gross / (Decimal("1") + tax_fraction))
            if mode == "inclusive" and rate > 0 else discount_gross
        )
        product_discount = min(product_discount, taxable_before_discount)
        subtotal += taxable_before_discount
        line_items.append({
            "product": product, "qty": qty, "rate": unit_rate,
            "gst_rate": rate, "gst_selection_type": product.gst_selection_type or "preset",
            "selling_price_tax_mode": mode, "taxable_before_discount": taxable_before_discount,
            "product_discount": product_discount,
        })

    # Invoice-level discount is allocated over taxable line values; GST is
    # then calculated once at each product's snapshotted rate.
    invoice_discount = min(_money(payload.discount), max(Decimal("0"), subtotal - sum(x["product_discount"] for x in line_items)))
    product_discount_total = sum(x["product_discount"] for x in line_items)
    discount = product_discount_total + invoice_discount
    remaining_discount = discount
    remaining_taxable = subtotal
    tax = Decimal("0")
    grand_total = Decimal("0")
    settings = db.query(models.BusinessSettings).filter(models.BusinessSettings.id == 1).first()
    seller_state_code = (settings.state_code if settings else "29") or "29"
    place_state_code = payload.place_of_supply_state_code or payload.customer_state_code or (farmer.state_code if farmer else None) or seller_state_code
    is_intrastate = str(place_state_code).zfill(2) == str(seller_state_code).zfill(2)
    cgst_total = sgst_total = igst_total = Decimal("0")
    for index, line in enumerate(line_items):
        before = line["taxable_before_discount"]
        allocated_invoice_discount = (
            invoice_discount if index == len(line_items) - 1 else
            min(before - line["product_discount"], _money(invoice_discount * (before-line["product_discount"]) / max(Decimal("0.01"), subtotal-product_discount_total)))
        )
        allocated_discount = line["product_discount"] + allocated_invoice_discount
        remaining_discount -= allocated_discount
        taxable_line = max(Decimal("0"), before - allocated_discount)
        line_tax = _money(taxable_line * line["gst_rate"] / Decimal("100"))
        line_total = _money(taxable_line + line_tax)
        if is_intrastate:
            line_cgst = _money(line_tax / 2)
            line_sgst = line_tax - line_cgst
            line_igst = Decimal("0")
        else:
            line_cgst = line_sgst = Decimal("0")
            line_igst = line_tax
        line.update(taxable_amount=taxable_line, tax_amount=line_tax, total_amount=line_total,
                    discount_amount=allocated_discount, cgst_amount=line_cgst, sgst_amount=line_sgst,
                    igst_amount=line_igst)
        tax += line_tax
        grand_total += line_total
    tax = _money(tax)
    raw_total = _money(grand_total)
    grand_total = raw_total.quantize(Decimal("1"), rounding=ROUND_HALF_UP).quantize(MONEY)
    rounding_adjustment = grand_total - raw_total
    cgst_total = _money(sum(line["cgst_amount"] for line in line_items))
    sgst_total = _money(sum(line["sgst_amount"] for line in line_items))
    igst_total = _money(sum(line["igst_amount"] for line in line_items))
    taxable_amount = max(Decimal("0"), subtotal - discount)
    amount_paid = _money(payload.amount_paid)

    if payload.payment_method == models.PaymentMethodEnum.credit and amount_paid == 0:
        status_ = models.InvoiceStatusEnum.credit
    elif amount_paid >= grand_total:
        status_ = models.InvoiceStatusEnum.paid
        amount_paid = grand_total  # don't overpay on record
    elif amount_paid <= 0:
        status_ = models.InvoiceStatusEnum.credit
    else:
        status_ = models.InvoiceStatusEnum.partial

    balance_due = max(Decimal("0"), grand_total - amount_paid)

    invoice = models.Invoice(
        # The numeric primary key, assigned by the database, is the only
        # concurrency-safe invoice sequence. A temporary unique value lets us
        # flush first and then format the customer-facing number.
        invoice_no=f"PENDING-{uuid4()}",
        client_request_id=client_request_id,
        client_payload_hash=hashlib.sha256(payload.model_dump_json().encode("utf-8")).hexdigest(),
        farmer_id=payload.farmer_id,
        user_id=current_user.id,
        date=datetime.now(UTC).replace(tzinfo=None),
        subtotal=subtotal,
        discount=discount,
        invoice_level_discount=invoice_discount,
        tax=tax,
        grand_total=grand_total,
        payment_method=payload.payment_method,
        amount_paid=amount_paid,
        balance_due=balance_due,
        status=status_,
        customer_name=payload.customer_name or (farmer.name if farmer else "Walk-in Customer"),
        customer_address=payload.customer_address or ((farmer.delivery_address or farmer.billing_address or farmer.location) if farmer else None),
        customer_gstin=payload.customer_gstin or (farmer.gstin if farmer else None),
        customer_phone=payload.customer_phone or (farmer.mobile if farmer else None),
        customer_state_name=payload.customer_state_name or (farmer.state_name if farmer else None),
        customer_state_code=payload.customer_state_code or (farmer.state_code if farmer else None),
        place_of_supply_state_name=payload.place_of_supply_state_name or payload.customer_state_name or (farmer.state_name if farmer else None) or (settings.state_name if settings else "Karnataka"),
        place_of_supply_state_code=place_state_code,
        cgst_amount=cgst_total, sgst_amount=sgst_total, igst_amount=igst_total,
        rounding_adjustment=rounding_adjustment, amount_in_words=amount_in_words(grand_total),
        remarks=payload.remarks, payment_terms=payload.payment_terms, due_date=payload.due_date,
        advance_amount=amount_paid, invoice_type=payload.invoice_type,
    )
    db.add(invoice)
    db.flush()  # get invoice.id before adding items
    invoice.invoice_no = f"INV-{invoice.id:06d}"

    for line in line_items:
        product, qty = line["product"], line["qty"]
        previous_stock = Decimal(product.stock)
        new_stock = previous_stock - Decimal(str(qty))
        db.add(
            models.InvoiceItem(
                invoice_id=invoice.id,
                product_id=product.id,
                product_name=product.name,
                unit=product.unit,
                qty=qty,
                rate=line["rate"],
                amount=line["total_amount"],
                gst_rate=line["gst_rate"],
                gst_selection_type=line["gst_selection_type"],
                selling_price_tax_mode=line["selling_price_tax_mode"],
                taxable_amount=line["taxable_amount"],
                tax_amount=line["tax_amount"],
                total_amount=line["total_amount"],
                hsn_code=product.hsn_code,
                discount_amount=line["discount_amount"],
                cgst_amount=line["cgst_amount"],
                sgst_amount=line["sgst_amount"],
                igst_amount=line["igst_amount"],
                purchase_rate_snapshot=product.purchase_price_including_gst,
                purchase_cost_ex_tax_snapshot=product.purchase_price_excluding_gst,
                purchase_cost_incl_tax_snapshot=product.purchase_price_including_gst,
            )
        )
        product.stock = new_stock
        db.add(models.StockMovement(
            product_id=product.id, movement_type="SALE", quantity_in=0, quantity_out=qty,
            previous_stock=previous_stock, new_stock=new_stock, reference_number=invoice.invoice_no,
            notes=f"Stock deducted for {invoice.invoice_no}", created_by=current_user.id,
        ))

    if amount_paid > 0:
        db.add(models.Payment(
            invoice_id=invoice.id,
            user_id=current_user.id,
            amount=amount_paid,
            method=payload.payment_method,
        ))

    db.commit()
    db.refresh(invoice)
    return invoice


@router.delete("/{invoice_id}", status_code=204)
def delete_invoice(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_roles("admin")),
):
    """Remove an erroneous invoice and return its quantities to inventory."""
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        invoice = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first()
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        invoice_number = invoice.invoice_no
        for item in invoice.items:
            if item.product_id is not None:
                product = db.query(models.Product).filter(models.Product.id == item.product_id).first()
                if product:
                    previous_stock = Decimal(product.stock)
                    new_stock = previous_stock + Decimal(item.qty)
                    product.stock = new_stock
                    db.add(models.StockMovement(
                        product_id=product.id, movement_type="SALE_RETURN", quantity_in=item.qty,
                        quantity_out=0, previous_stock=previous_stock, new_stock=new_stock,
                        reference_number=invoice_number, notes=f"Invoice {invoice_number} voided; stock restored",
                        created_by=current_user.id,
                    ))
        db.delete(invoice)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    return None


@router.post("/{invoice_id}/payments", response_model=schemas.InvoiceOut)
def record_payment(
    invoice_id: int,
    payload: schemas.PaymentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        invoice = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first()
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        amount = _money(payload.amount)
        if payload.method == models.PaymentMethodEnum.credit:
            raise HTTPException(status_code=400, detail="Credit is not a received payment method")
        if amount > Decimal(invoice.balance_due):
            raise HTTPException(status_code=400, detail="Payment cannot exceed the outstanding balance")

        db.add(models.Payment(
            invoice_id=invoice.id, user_id=current_user.id, amount=amount, method=payload.method
        ))
        invoice.amount_paid = _money(Decimal(invoice.amount_paid) + amount)
        invoice.balance_due = _money(Decimal(invoice.grand_total) - Decimal(invoice.amount_paid))
        invoice.status = (
            models.InvoiceStatusEnum.paid if invoice.balance_due == 0
            else models.InvoiceStatusEnum.partial
        )
        db.commit()
        db.refresh(invoice)
        return invoice
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.get("", response_model=List[schemas.InvoiceListOut])
def list_invoices(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    invoices = (
        db.query(models.Invoice)
        .options(joinedload(models.Invoice.farmer))
        .order_by(models.Invoice.date.desc())
        .all()
    )
    return [
        schemas.InvoiceListOut(
            id=i.id,
            invoice_no=i.invoice_no,
            farmer_id=i.farmer_id,
            farmer_name=i.customer_name or (i.farmer.name if i.farmer else "Walk-in Customer"),
            date=i.date,
            grand_total=i.grand_total,
            amount_paid=i.amount_paid,
            balance_due=i.balance_due,
            payment_method=i.payment_method,
            status=i.status,
        )
        for i in invoices
    ]


@router.get("/{invoice_id}", response_model=schemas.InvoiceOut)
def get_invoice(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    invoice = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


@router.patch("/{invoice_id}/electronic-details", response_model=schemas.InvoiceOut)
def record_electronic_invoice_details(
    invoice_id: int,
    payload: schemas.InvoiceElectronicDetailsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.require_roles("admin")),
):
    """Store real IRP/e-way identifiers returned by an authorized provider.

    This endpoint does not generate IRNs, acknowledgements, or e-way bills.
    """
    invoice = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(invoice, key, value)
    db.commit()
    db.refresh(invoice)
    return invoice
