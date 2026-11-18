"""Internal sales-cost and CA accounting exports; never exposed to staff."""
from datetime import date, datetime, time
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from xml.sax.saxutils import escape

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import Response
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, A4, landscape
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from sqlalchemy.orm import Session, joinedload

from .. import auth, models
from ..database import get_db
from .reports import _NumberedCanvas, _PDF_FONT, _PDF_BOLD_FONT

router = APIRouter(prefix="/accounting", tags=["Internal Accounting"])


def _query_rows(db, start_date, end_date):
    q = db.query(models.Invoice).options(
        joinedload(models.Invoice.items).joinedload(models.InvoiceItem.product),
        joinedload(models.Invoice.farmer),
    ).order_by(models.Invoice.date, models.Invoice.id)
    if start_date:
        q = q.filter(models.Invoice.date >= datetime.combine(start_date, time.min))
    if end_date:
        q = q.filter(models.Invoice.date <= datetime.combine(end_date, time.max))
    result = []
    for invoice in q.all():
        for item in invoice.items:
            buy_incl = Decimal(str(item.purchase_cost_incl_tax_snapshot)) if item.purchase_cost_incl_tax_snapshot is not None else None
            buy_ex = Decimal(str(item.purchase_cost_ex_tax_snapshot)) if item.purchase_cost_ex_tax_snapshot is not None else None
            product = item.product
            master_buy = Decimal(str(product.purchase_price_including_gst)) if product and product.purchase_price_including_gst is not None else None
            master_sell_value = product.selling_price if product and product.selling_price is not None else (product.rate if product else None)
            master_sell = Decimal(str(master_sell_value)) if master_sell_value is not None else None
            # Actual purchase rate is kept tax-exclusive for cost and margin reporting.
            actual_buy = buy_ex
            sales = Decimal(str(item.taxable_amount if item.taxable_amount is not None else item.total_amount if item.total_amount is not None else item.amount))
            cost_basis = buy_ex if buy_ex is not None else buy_incl
            purchase_value = (cost_basis * Decimal(item.qty)).quantize(Decimal("0.01")) if cost_basis is not None else None
            margin = (sales - purchase_value) if purchase_value is not None else None
            result.append({
                "transaction_type": "SALE",
                "line_id": item.id,
                "invoice_number": invoice.invoice_no, "transaction_date": invoice.date,
                "customer_name": invoice.customer_name or (invoice.farmer.name if invoice.farmer else "Walk-in Customer"),
                "product_name": item.product_name, "quantity": Decimal(item.qty),
                "buying_price": master_buy, "selling_price": master_sell,
                "actual_purchase_rate": actual_buy,
                "actual_sales_rate": Decimal(item.rate), "purchase_value": purchase_value,
                "sales_value": sales, "gross_margin": margin,
                "discount_amount": Decimal(str(item.discount_amount or 0)),
                "sales_total": Decimal(str(item.total_amount if item.total_amount is not None else item.amount)),
                "hsn_code": item.hsn_code, "gst_rate": Decimal(str(item.gst_rate or 0)),
                "taxable_value": Decimal(str(item.taxable_amount or 0)),
                "cgst": Decimal(str(item.cgst_amount or 0)), "sgst": Decimal(str(item.sgst_amount or 0)),
                "igst": Decimal(str(item.igst_amount or 0)), "cost_snapshot_available": purchase_value is not None,
            })
    return result


def _purchase_rows(db, start_date=None, end_date=None):
    query = db.query(models.PurchaseEntry, models.Product).join(
        models.Product, models.Product.id == models.PurchaseEntry.product_id
    ).order_by(models.PurchaseEntry.purchase_date, models.PurchaseEntry.id)
    if start_date:
        query = query.filter(models.PurchaseEntry.purchase_date >= datetime.combine(start_date, time.min))
    if end_date:
        query = query.filter(models.PurchaseEntry.purchase_date <= datetime.combine(end_date, time.max))
    rows = []
    for entry, product in query.all():
        gst_rate = Decimal(str(entry.gst_rate)) if entry.gst_rate is not None else None
        total = Decimal(str(entry.total_amount))
        taxable = gst_amount = None
        if gst_rate is not None:
            taxable = (total / (Decimal("1") + gst_rate / Decimal("100"))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            gst_amount = total - taxable
        rows.append({
            "transaction_type": "PURCHASE",
            "invoice_number": entry.invoice_number or f"PUR-{entry.id:06d}",
            "transaction_date": entry.purchase_date,
            "customer_name": entry.supplier or "Supplier not recorded",
            "product_id": entry.product_id,
            "product_name": entry.product_name or product.name,
            "quantity": Decimal(str(entry.quantity)),
            "buying_price": Decimal(str(entry.master_buying_price_snapshot)) if entry.master_buying_price_snapshot is not None else None,
            "selling_price": Decimal(str(entry.master_selling_price_snapshot)) if entry.master_selling_price_snapshot is not None else None,
            "actual_purchase_rate": Decimal(str(entry.unit_price_including_gst)),
            "actual_sales_rate": None,
            "purchase_value": total,
            "purchase_taxable_value": taxable,
            "purchase_gst_amount": gst_amount,
            "sales_value": None,
            "sales_total": None,
            "gross_margin": None,
            "discount_amount": Decimal("0"),
            "hsn_code": entry.hsn_code or product.hsn_code,
            "gst_rate": gst_rate,
            "taxable_value": taxable,
            "cgst": None, "sgst": None, "igst": None,
            "cost_snapshot_available": True,
        })
    return rows


def _filter_report_rows(rows, invoice_search=None, product_search=None, customer_search=None,
                        transaction_type="all", missing_cost_snapshot="all"):
    invoice_text = (invoice_search or "").strip().casefold()
    product_text = (product_search or "").strip().casefold()
    customer_text = (customer_search or "").strip().casefold()
    transaction_type = (transaction_type or "all").strip().casefold()
    missing_cost_snapshot = (missing_cost_snapshot or "all").strip().casefold()
    if transaction_type not in {"all", "sale", "purchase"}:
        raise HTTPException(422, "transaction_type must be all, sale, or purchase")
    if missing_cost_snapshot not in {"all", "missing", "saved"}:
        raise HTTPException(422, "missing_cost_snapshot must be all, missing, or saved")
    filtered = []
    for row in rows:
        kind = row["transaction_type"].casefold()
        if transaction_type != "all" and kind != transaction_type:
            continue
        if invoice_text and invoice_text not in str(row["invoice_number"]).casefold():
            continue
        if product_text and product_text not in str(row["product_name"]).casefold():
            continue
        if customer_text and customer_text not in str(row.get("customer_name") or "").casefold():
            continue
        has_snapshot = bool(row["cost_snapshot_available"]) if kind == "sale" else True
        if missing_cost_snapshot == "missing" and (kind != "sale" or has_snapshot):
            continue
        if missing_cost_snapshot == "saved" and not has_snapshot:
            continue
        filtered.append(row)
    return filtered


def _accounting_export_filters(rows, purchase_rows, invoice_search, product_search,
                               customer_search, transaction_type, missing_cost_snapshot):
    rows = _filter_report_rows(rows, invoice_search, product_search, customer_search,
                               transaction_type, missing_cost_snapshot)
    purchase_rows = _filter_report_rows(purchase_rows, invoice_search, product_search, customer_search,
                                        transaction_type, missing_cost_snapshot)
    return rows, purchase_rows


def _product_master_rows(db):
    products = db.query(models.Product).order_by(models.Product.catalog_serial, models.Product.sku).all()
    rows = []
    for product in products:
        sell = product.selling_price if product.selling_price is not None else product.rate
        rows.append({
            "catalog_serial": product.catalog_serial,
            "name": product.name,
            "description": product.description,
            "hsn_code": product.hsn_code,
            "gst_rate": Decimal(str(product.gst_rate)) if product.gst_rate is not None else None,
            "unit": product.unit,
            "initial_quantity": Decimal(str(product.initial_quantity)) if product.initial_quantity is not None else None,
            "stock": Decimal(str(product.stock or 0)),
            "buying_price": Decimal(str(product.purchase_price_including_gst)) if product.purchase_price_including_gst is not None else None,
            "selling_price": Decimal(str(sell)) if sell is not None else None,
            "sku": product.sku,
            "is_active": bool(product.is_active),
        })
    return rows


def _require_accounting(user: models.User = Depends(auth.require_roles("admin", "accountant"))):
    return user


@router.get("/rows")
def accounting_rows(start_date: date | None = None, end_date: date | None = None,
                    invoice_search: str | None = None, product_search: str | None = None,
                    customer_search: str | None = None, transaction_type: str = "all",
                    missing_cost_snapshot: str = "all",
                    db: Session = Depends(get_db), current_user: models.User = Depends(_require_accounting)):
    if start_date and end_date and end_date < start_date:
        raise HTTPException(422, "end_date cannot be before start_date")
    rows = _query_rows(db, start_date, end_date) + _purchase_rows(db, start_date, end_date)
    rows = _filter_report_rows(rows, invoice_search, product_search, customer_search,
                               transaction_type, missing_cost_snapshot)
    rows.sort(key=lambda row: (row["transaction_date"], row["invoice_number"], row["product_name"]))
    return [{key: (float(value) if isinstance(value, Decimal) else value.isoformat() if isinstance(value, datetime) else value)
             for key, value in row.items()} for row in rows]


@router.post("/lines/{line_id}/cost-correction", status_code=200)
def correct_historical_cost(line_id: int, payload: dict = Body(...),
                            db: Session = Depends(get_db), current_user: models.User = Depends(_require_accounting)):
    item = db.query(models.InvoiceItem).filter(models.InvoiceItem.id == line_id).first()
    if item is None:
        raise HTTPException(404, "Invoice line not found")
    if item.purchase_cost_ex_tax_snapshot is not None:
        raise HTTPException(409, "This invoice line already has a saved cost snapshot")
    try:
        ex_tax = Decimal(str(payload.get("unit_cost_ex_tax", "")))
        incl_raw = payload.get("unit_cost_incl_gst")
        incl_tax = Decimal(str(incl_raw)) if incl_raw not in (None, "") else None
    except Exception as exc:
        raise HTTPException(422, "Enter valid decimal unit cost values") from exc
    source = str(payload.get("source_reference") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if not ex_tax.is_finite() or ex_tax < 0 or ex_tax > Decimal("9999999999.99"):
        raise HTTPException(422, "Excluding-GST cost must be a valid non-negative amount")
    if incl_tax is not None and (not incl_tax.is_finite() or incl_tax < 0 or incl_tax > Decimal("9999999999.99")):
        raise HTTPException(422, "Including-GST rate must be a valid non-negative amount")
    if not source or len(source) > 200:
        raise HTTPException(422, "A source document reference of 1–200 characters is required")
    if not reason or len(reason) > 1000:
        raise HTTPException(422, "A correction reason of 1–1000 characters is required")
    ex_tax = ex_tax.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if incl_tax is not None:
        incl_tax = incl_tax.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    correction = models.InvoiceCostCorrection(
        invoice_item_id=item.id,
        previous_cost_ex_tax=item.purchase_cost_ex_tax_snapshot,
        corrected_cost_ex_tax=ex_tax,
        previous_cost_incl_tax=item.purchase_cost_incl_tax_snapshot,
        corrected_cost_incl_tax=incl_tax,
        source_reference=source,
        reason=reason,
        corrected_by=current_user.id,
    )
    item.purchase_cost_ex_tax_snapshot = ex_tax
    if incl_tax is not None:
        item.purchase_cost_incl_tax_snapshot = incl_tax
        item.purchase_rate_snapshot = incl_tax
    db.add(correction)
    db.commit()
    return {"message": "Historical cost snapshot saved with an audit record", "line_id": item.id,
            "correction_id": correction.id}


@router.get("/purchase-rows")
def accounting_purchase_rows(start_date: date | None = None, end_date: date | None = None,
                             db: Session = Depends(get_db), current_user: models.User = Depends(_require_accounting)):
    if start_date and end_date and end_date < start_date:
        raise HTTPException(422, "end_date cannot be before start_date")
    rows = _purchase_rows(db, start_date, end_date)
    return [{key: (float(value) if isinstance(value, Decimal) else value.isoformat() if isinstance(value, datetime) else value)
             for key, value in row.items()} for row in rows]


@router.get("/purchases/export")
def accounting_purchase_export(format: str = Query("xlsx", pattern="^(xlsx|pdf)$"),
                               start_date: date | None = None, end_date: date | None = None,
                               db: Session = Depends(get_db), current_user: models.User = Depends(_require_accounting)):
    if start_date and end_date and end_date < start_date:
        raise HTTPException(422, "end_date cannot be before start_date")
    rows = _purchase_rows(db, start_date, end_date)
    headers = ["Purchase Reference", "Purchase Date", "Product Name", "HSN/SAC", "Quantity",
               "Actual Buying Rate (incl. GST)", "Master Buying Price Snapshot", "Selling Price Reference",
               "GST Rate", "Taxable Purchase", "Input GST", "Total Purchase"]
    if format == "xlsx":
        book = Workbook()
        sheet = book.active
        sheet.title = "Purchases"
        sheet.append(["ADINATH AGRI SERVICES — Purchase Transactions"])
        sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
        sheet.append(headers)
        for row in rows:
            sheet.append([
                row["invoice_number"], row["transaction_date"].replace(tzinfo=None), row["product_name"],
                row["hsn_code"], float(row["quantity"]), float(row["actual_purchase_rate"]),
                float(row["buying_price"]) if row["buying_price"] is not None else None,
                float(row["selling_price"]) if row["selling_price"] is not None else None,
                float(row["gst_rate"]) if row["gst_rate"] is not None else None,
                float(row["purchase_taxable_value"]) if row["purchase_taxable_value"] is not None else None,
                float(row["purchase_gst_amount"]) if row["purchase_gst_amount"] is not None else None,
                float(row["purchase_value"]),
            ])
        for cell in sheet[2]:
            cell.fill = PatternFill("solid", fgColor="356C47")
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(wrap_text=True)
        for idx, width in enumerate([22, 18, 36, 15, 12, 24, 25, 23, 11, 20, 18, 20], 1):
            sheet.column_dimensions[__import__("openpyxl").utils.get_column_letter(idx)].width = width
        sheet.freeze_panes = "A3"
        sheet.auto_filter.ref = f"A2:L{sheet.max_row}"
        buf = BytesIO()
        book.save(buf)
        return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": "attachment; filename=Adinath_Purchase_Report.xlsx"})

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=8*mm, rightMargin=8*mm,
                            topMargin=9*mm, bottomMargin=16*mm)
    base = getSampleStyleSheet()
    small = ParagraphStyle("purchase", parent=base["Normal"], fontName=_PDF_FONT, fontSize=6.5, leading=8)
    header = ParagraphStyle("purchaseheader", parent=small, fontName=_PDF_BOLD_FONT, textColor=colors.white, alignment=1)
    title = ParagraphStyle("purchasetitle", parent=base["Title"], fontName=_PDF_BOLD_FONT,
                           fontSize=15, textColor=colors.HexColor("#1F5B3B"))
    data = [[Paragraph(label, header) for label in headers]]
    for row in rows:
        data.append([
            Paragraph(escape(str(row["invoice_number"])), small),
            row["transaction_date"].strftime("%d-%b-%Y"), Paragraph(escape(str(row["product_name"])), small),
            escape(str(row["hsn_code"] or "-")), f"{row['quantity']:,.2f}", f"{row['actual_purchase_rate']:,.2f}",
            f"{row['buying_price']:,.2f}" if row["buying_price"] is not None else "-",
            f"{row['selling_price']:,.2f}" if row["selling_price"] is not None else "-",
            f"{row['gst_rate']}%" if row["gst_rate"] is not None else "-",
            f"{row['purchase_taxable_value']:,.2f}" if row["purchase_taxable_value"] is not None else "-",
            f"{row['purchase_gst_amount']:,.2f}" if row["purchase_gst_amount"] is not None else "-",
            f"{row['purchase_value']:,.2f}",
        ])
    table = Table(data, colWidths=[22*mm, 18*mm, 38*mm, 15*mm, 12*mm, 25*mm, 25*mm, 23*mm, 12*mm, 22*mm, 19*mm, 22*mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1F5B3B")),
        ("FONTNAME", (0,0), (-1,-1), _PDF_FONT), ("FONTSIZE", (0,0), (-1,-1), 6.5),
        ("GRID", (0,0), (-1,-1), .25, colors.HexColor("#B7C9B4")),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"), ("ALIGN", (4,1), (-1,-1), "RIGHT"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#EEF4EB")]), ("PADDING", (0,0), (-1,-1), 3),
    ]))
    doc.build([Paragraph("ADINATH AGRI SERVICES — Purchase Transactions", title), Spacer(1, 3*mm), table], canvasmaker=_NumberedCanvas)
    return Response(buf.getvalue().rstrip(b"\r\n"), media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=Adinath_Purchase_Report.pdf"})


@router.get("/export")
def accounting_export(format: str = Query("xlsx", pattern="^(xlsx|pdf)$"), start_date: date | None = None,
                      end_date: date | None = None, invoice_search: str | None = None,
                      product_search: str | None = None, customer_search: str | None = None,
                      transaction_type: str = "all", missing_cost_snapshot: str = "all",
                      db: Session = Depends(get_db),
                      current_user: models.User = Depends(_require_accounting)):
    if start_date and end_date and end_date < start_date:
        raise HTTPException(422, "end_date cannot be before start_date")
    rows = _query_rows(db, start_date, end_date)
    purchase_rows = _purchase_rows(db, start_date, end_date)
    rows, purchase_rows = _accounting_export_filters(
        rows, purchase_rows, invoice_search, product_search, customer_search,
        transaction_type, missing_cost_snapshot,
    )
    product_rows = _product_master_rows(db)
    missing = sum(1 for row in rows if not row["cost_snapshot_available"])
    if format == "xlsx":
        book = Workbook(); sheet = book.active; sheet.title = "CA Accounting"
        headers = ["Invoice Number", "Transaction Date", "Product Name", "Quantity", "Buying Price (Master, incl. GST)", "Selling Price (Master)", "Purchase Cost Snapshot at Sale (excl. GST)", "Actual Sales Rate (saved)", "Purchase Value (excl. GST)", "Sales Value (taxable)", "Gross Margin", "HSN/SAC", "GST Rate", "Taxable Value", "CGST", "SGST", "IGST", "Purchase Cost Snapshot Status", "Line Discount", "Sales Total (incl. GST)"]
        sheet.append(["ADINATH AGRI SERVICES — Internal Accounting Report"])
        sheet.merge_cells(start_row=1,start_column=1,end_row=1,end_column=len(headers))
        sheet.cell(1,1).fill=PatternFill("solid",fgColor="1F5B3B"); sheet.cell(1,1).font=Font(color="FFFFFF",bold=True,size=14); sheet.cell(1,1).alignment=Alignment(horizontal="center")
        sheet.row_dimensions[1].height=24
        sheet.append(["Master buying and selling prices are shown separately from saved transaction rates. Historical rows without a saved purchase-cost snapshot remain blank; no cost is reconstructed."])
        sheet.merge_cells(start_row=2,start_column=1,end_row=2,end_column=len(headers))
        sheet.cell(2,1).font=Font(italic=True,color="526B5A")
        sheet.row_dimensions[2].height=30
        sheet.append(headers)
        for cell in sheet[3]: cell.fill=PatternFill("solid",fgColor="356C47"); cell.font=Font(color="FFFFFF",bold=True); cell.alignment=Alignment(wrap_text=True)
        sheet.row_dimensions[3].height=38
        for row in rows:
            sheet.append([row["invoice_number"], row["transaction_date"].replace(tzinfo=None), row["product_name"], float(row["quantity"]),
                          float(row["buying_price"]) if row["buying_price"] is not None else None,
                          float(row["selling_price"]) if row["selling_price"] is not None else None,
                          float(row["actual_purchase_rate"]) if row["actual_purchase_rate"] is not None else None,
                          float(row["actual_sales_rate"]), float(row["purchase_value"]) if row["purchase_value"] is not None else None,
                          float(row["sales_value"]), float(row["gross_margin"]) if row["gross_margin"] is not None else None,
                          row["hsn_code"], float(row["gst_rate"]), float(row["taxable_value"]), float(row["cgst"]), float(row["sgst"]), float(row["igst"]),
                          "Available" if row["cost_snapshot_available"] else "Missing historical snapshot",
                          float(row["discount_amount"]), float(row["sales_total"])])
        widths=[20,18,34,12,24,19,29,20,19,19,16,14,11,16,12,12,12,28,16,20]
        for i,width in enumerate(widths,1): sheet.column_dimensions[__import__('openpyxl').utils.get_column_letter(i)].width=width
        sheet.freeze_panes="A4"; sheet.auto_filter.ref=f"A3:T{sheet.max_row}"

        purchase_sheet = book.create_sheet("Purchase Transactions")
        purchase_headers = ["Purchase Reference", "Purchase Date", "Product Name", "HSN/SAC", "Quantity", "Actual Buying Rate (incl. GST)", "Master Buying Price Snapshot", "Selling Price Reference", "GST Rate", "Taxable Purchase", "Input GST", "Total Purchase"]
        purchase_sheet.append(["Actual purchase rates are recorded per supplier transaction and do not overwrite Product Master prices."])
        purchase_sheet.merge_cells(start_row=1,start_column=1,end_row=1,end_column=len(purchase_headers))
        purchase_sheet.append(purchase_headers)
        for row in purchase_rows:
            purchase_sheet.append([row["invoice_number"], row["transaction_date"].replace(tzinfo=None), row["product_name"], row["hsn_code"],
                float(row["quantity"]), float(row["actual_purchase_rate"]),
                float(row["buying_price"]) if row["buying_price"] is not None else None,
                float(row["selling_price"]) if row["selling_price"] is not None else None,
                float(row["gst_rate"]) if row["gst_rate"] is not None else None,
                float(row["purchase_taxable_value"]) if row["purchase_taxable_value"] is not None else None,
                float(row["purchase_gst_amount"]) if row["purchase_gst_amount"] is not None else None,
                float(row["purchase_value"])])
        for cell in purchase_sheet[2]:
            cell.fill=PatternFill("solid",fgColor="356C47"); cell.font=Font(color="FFFFFF",bold=True); cell.alignment=Alignment(wrap_text=True)
        for i,width in enumerate([22,18,36,15,12,24,25,23,11,20,18,20],1): purchase_sheet.column_dimensions[__import__('openpyxl').utils.get_column_letter(i)].width=width
        purchase_sheet.freeze_panes="A3"; purchase_sheet.auto_filter.ref=f"A2:L{purchase_sheet.max_row}"

        master_sheet = book.create_sheet("Product Master")
        master_headers = ["PDF Serial", "SKU", "Product Name", "Description", "HSN/SAC", "GST Rate", "Unit", "Opening Quantity", "Current Stock", "Buying Price (incl. GST)", "Selling Price", "Active"]
        master_sheet.append(["Descriptions absent from the supplied PDF remain blank until entered in Product Management."])
        master_sheet.merge_cells(start_row=1,start_column=1,end_row=1,end_column=len(master_headers))
        master_sheet.append(master_headers)
        for row in product_rows:
            master_sheet.append([row["catalog_serial"],row["sku"],row["name"],row["description"],row["hsn_code"],
                float(row["gst_rate"]) if row["gst_rate"] is not None else None,row["unit"],
                float(row["initial_quantity"]) if row["initial_quantity"] is not None else None,float(row["stock"]),
                float(row["buying_price"]) if row["buying_price"] is not None else None,
                float(row["selling_price"]) if row["selling_price"] is not None else None,row["is_active"]])
        for cell in master_sheet[2]:
            cell.fill=PatternFill("solid",fgColor="356C47"); cell.font=Font(color="FFFFFF",bold=True); cell.alignment=Alignment(wrap_text=True)
        for i,width in enumerate([12,16,38,36,15,12,12,16,15,23,20,10],1): master_sheet.column_dimensions[__import__('openpyxl').utils.get_column_letter(i)].width=width
        master_sheet.freeze_panes="A3"; master_sheet.auto_filter.ref=f"A2:L{master_sheet.max_row}"

        summary_sheet = book.create_sheet("Summary")
        total_sales = sum((row["sales_value"] for row in rows), Decimal("0"))
        total_purchase_cost = sum((row["purchase_value"] for row in rows if row["purchase_value"] is not None), Decimal("0"))
        total_purchase_transactions = sum((row["purchase_value"] for row in purchase_rows), Decimal("0"))
        known_sales = sum((row["sales_value"] for row in rows if row["cost_snapshot_available"]), Decimal("0"))
        known_margin = sum((row["gross_margin"] for row in rows if row["gross_margin"] is not None), Decimal("0"))
        summary_rows = [
            ["ADINATH AGRI SERVICES — CA Accounting Summary"],
            ["Report period", f"{start_date or 'All dates'} to {end_date or 'All dates'}"],
            ["Transaction type", transaction_type.title()],
            ["Total sales value (taxable)", float(total_sales)],
            ["Actual purchase transactions (incl. GST)", float(total_purchase_transactions)],
            ["Sales cost snapshots (excl. GST)", float(total_purchase_cost)],
            ["Gross margin on saved cost lines", float(known_margin)],
            ["Gross margin percentage on saved cost lines", float((known_margin / known_sales * 100) if known_sales else 0)],
            ["Sales invoice lines", len(rows)],
            ["Missing cost snapshots", missing],
            ["Margin coverage sales lines", sum(1 for row in rows if row["cost_snapshot_available"])],
            ["Note", "Missing historical costs are not estimated from current Product Master prices."],
        ]
        for row in summary_rows:
            summary_sheet.append(row)
        summary_sheet.column_dimensions["A"].width = 48
        summary_sheet.column_dimensions["B"].width = 86
        summary_sheet["A1"].fill = PatternFill("solid", fgColor="1F5B3B")
        summary_sheet["A1"].font = Font(color="FFFFFF", bold=True, size=14)
        summary_sheet.freeze_panes = "A2"
        for row in range(4, 9):
            summary_sheet.cell(row, 2).number_format = '₹#,##0.00;[Red]-₹#,##0.00'
        buf=BytesIO(); book.save(buf)
        return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition":"attachment; filename=Adinath_CA_Accounting.xlsx"})

    buf = BytesIO(); doc = SimpleDocTemplate(buf, pagesize=landscape(A3), leftMargin=8*mm, rightMargin=8*mm, topMargin=9*mm, bottomMargin=16*mm)
    base=getSampleStyleSheet()
    small=ParagraphStyle("acct",parent=base["Normal"],fontName=_PDF_FONT,fontSize=6.4,leading=7.6)
    header=ParagraphStyle("acctheader",parent=small,fontName=_PDF_BOLD_FONT,fontSize=6.2,leading=7.1,textColor=colors.white,alignment=1)
    title=ParagraphStyle("accttitle",parent=base["Title"],fontName=_PDF_BOLD_FONT,fontSize=15,textColor=colors.HexColor("#1F5B3B"))
    total_sales = sum((row["sales_value"] for row in rows), Decimal("0"))
    total_purchase_cost = sum((row["purchase_value"] for row in rows if row["purchase_value"] is not None), Decimal("0"))
    total_purchase_transactions = sum((row["purchase_value"] for row in purchase_rows), Decimal("0"))
    known_sales = sum((row["sales_value"] for row in rows if row["cost_snapshot_available"]), Decimal("0"))
    known_margin = sum((row["gross_margin"] for row in rows if row["gross_margin"] is not None), Decimal("0"))
    margin_percent = (known_margin / known_sales * 100) if known_sales else Decimal("0")
    story=[Paragraph("ADINATH AGRI SERVICES - Internal CA Accounting Report",title),
           Paragraph(f"Report period: {start_date or 'All dates'} to {end_date or 'All dates'} | Sales lines: {len(rows)} | Missing cost snapshots: {missing}. Current master prices are references; actual rates come from saved transaction records. Missing historical costs are left blank.",small),
           Paragraph(f"Sales revenue (taxable): ₹{total_sales:,.2f} | Recorded purchase transactions: ₹{total_purchase_transactions:,.2f} | Saved cost on sales: ₹{total_purchase_cost:,.2f} | Gross margin on saved cost lines: ₹{known_margin:,.2f} ({margin_percent:.2f}%)",small),Spacer(1,3*mm)]
    headers=["Invoice / Date","Product","Qty","Buying Price<br/>(Master, incl. GST)","Selling Price<br/>(Master)","Purchase Cost Snapshot<br/>(excl. GST)","Actual Sales Rate","Discount","Purchase Cost<br/>(excl. GST)","Sales Taxable","Sales Total<br/>(incl. GST)","Gross Margin","HSN/SAC","GST","CGST","SGST","IGST"]
    data=[[Paragraph(label,header) for label in headers]]
    for r in rows:
        date_label = r["transaction_date"].strftime("%d-%b-%Y") if r["transaction_date"] else "-"
        data.append([Paragraph(f"{escape(str(r['invoice_number']))}<br/>{date_label}",small),Paragraph(escape(str(r["product_name"])),small),f"{r['quantity']:,.2f}",
                     f"{r['buying_price']:,.2f}" if r['buying_price'] is not None else "-",
                     f"{r['selling_price']:,.2f}" if r['selling_price'] is not None else "-",
                     f"{r['actual_purchase_rate']:,.2f}" if r['actual_purchase_rate'] is not None else "-",f"{r['actual_sales_rate']:,.2f}",
                     f"{r['discount_amount']:,.2f}",f"{r['purchase_value']:,.2f}" if r['purchase_value'] is not None else "-",
                     f"{r['sales_value']:,.2f}",f"{r['sales_total']:,.2f}",
                     f"{r['gross_margin']:,.2f}" if r['gross_margin'] is not None else "-",escape(str(r['hsn_code'] or "-")),f"{r['gst_rate']}%",
                     f"{r['cgst']:,.2f}",f"{r['sgst']:,.2f}",f"{r['igst']:,.2f}"])
    table=Table(data,colWidths=[28*mm,42*mm,11*mm,19*mm,18*mm,22*mm,18*mm,14*mm,19*mm,18*mm,19*mm,17*mm,17*mm,10*mm,13*mm,13*mm,13*mm],repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#1F5B3B")),("TEXTCOLOR",(0,0),(-1,0),colors.white),
                               ("FONTNAME",(0,0),(-1,0),_PDF_BOLD_FONT),("FONTNAME",(0,1),(-1,-1),_PDF_FONT),
                               ("FONTSIZE",(0,0),(-1,-1),6.4),("GRID",(0,0),(-1,-1),.25,colors.HexColor("#B7C9B4")),
                               ("VALIGN",(0,0),(-1,-1),"MIDDLE"),("ALIGN",(2,1),(-1,-1),"RIGHT"),
                               ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#EEF4EB")]),("PADDING",(0,0),(-1,-1),3)]))
    story.extend([table, PageBreak(), Paragraph("Purchase Transactions", base["Heading2"]),
                  Paragraph("Actual buying rates are recorded per supplier transaction; selling prices are reference snapshots only.", small), Spacer(1, 2*mm)])
    purchase_headers=["Reference / Date","Product","HSN/SAC","Qty","Actual Buying Rate<br/>(incl. GST)","Buying Price Master Snapshot","Selling Price Reference","GST","Taxable Purchase","Input GST","Total Purchase"]
    purchase_data=[[Paragraph(label,header) for label in purchase_headers]]
    for r in purchase_rows:
        date_label=r["transaction_date"].strftime("%d-%b-%Y") if r["transaction_date"] else "-"
        purchase_data.append([Paragraph(f"{escape(str(r['invoice_number']))}<br/>{date_label}",small),Paragraph(escape(str(r["product_name"])),small),
            escape(str(r["hsn_code"] or "-")),f"{r['quantity']:,.2f}",f"{r['actual_purchase_rate']:,.2f}",
            f"{r['buying_price']:,.2f}" if r["buying_price"] is not None else "-",
            f"{r['selling_price']:,.2f}" if r["selling_price"] is not None else "-",
            f"{r['gst_rate']}%" if r["gst_rate"] is not None else "-",
            f"{r['purchase_taxable_value']:,.2f}" if r["purchase_taxable_value"] is not None else "-",
            f"{r['purchase_gst_amount']:,.2f}" if r["purchase_gst_amount"] is not None else "-",f"{r['purchase_value']:,.2f}"])
    purchase_table=Table(purchase_data,colWidths=[28*mm,43*mm,18*mm,13*mm,29*mm,29*mm,26*mm,12*mm,24*mm,20*mm,24*mm],repeatRows=1)
    purchase_table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#1F5B3B")),("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTNAME",(0,0),(-1,0),_PDF_BOLD_FONT),("FONTNAME",(0,1),(-1,-1),_PDF_FONT),("FONTSIZE",(0,0),(-1,-1),6.5),
        ("GRID",(0,0),(-1,-1),.25,colors.HexColor("#B7C9B4")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("ALIGN",(3,1),(-1,-1),"RIGHT"),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#EEF4EB")]),
        ("PADDING",(0,0),(-1,-1),3)]))
    story.extend([purchase_table, PageBreak(), Paragraph("Product Master", base["Heading2"]),
                  Paragraph("Descriptions not present in the supplied PDF remain blank until entered in Product Management.", small), Spacer(1, 2*mm)])
    master_headers=["Serial / SKU","Product Name","Description","HSN/SAC","GST","Unit","Opening Qty","Current Stock","Buying Price<br/>(incl. GST)","Selling Price"]
    master_data=[[Paragraph(label,header) for label in master_headers]]
    for r in product_rows:
        serial_sku=f"{r['catalog_serial']} / {r['sku']}" if r["catalog_serial"] else r["sku"]
        master_data.append([escape(serial_sku),Paragraph(escape(str(r["name"])),small),Paragraph(escape(str(r["description"] or "")),small),
            escape(str(r["hsn_code"] or "-")),f"{r['gst_rate']}%" if r["gst_rate"] is not None else "-",escape(str(r["unit"])),
            f"{r['initial_quantity']:,.2f}" if r["initial_quantity"] is not None else "-",f"{r['stock']:,.2f}",
            f"{r['buying_price']:,.2f}" if r["buying_price"] is not None else "-",
            f"{r['selling_price']:,.2f}" if r["selling_price"] is not None else "-"])
    master_table=Table(master_data,colWidths=[31*mm,48*mm,54*mm,20*mm,14*mm,16*mm,22*mm,22*mm,31*mm,25*mm],repeatRows=1)
    master_table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#1F5B3B")),("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTNAME",(0,0),(-1,0),_PDF_BOLD_FONT),("FONTNAME",(0,1),(-1,-1),_PDF_FONT),("FONTSIZE",(0,0),(-1,-1),6.5),
        ("GRID",(0,0),(-1,-1),.25,colors.HexColor("#B7C9B4")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("ALIGN",(4,1),(-1,-1),"RIGHT"),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#EEF4EB")]),
        ("PADDING",(0,0),(-1,-1),3)]))
    story.append(master_table)
    doc.build(story,canvasmaker=_NumberedCanvas)
    return Response(buf.getvalue().rstrip(b"\r\n"),media_type="application/pdf",
                    headers={"Content-Disposition":"attachment; filename=Adinath_CA_Accounting.pdf"})
