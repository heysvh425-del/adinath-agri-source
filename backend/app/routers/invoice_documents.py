"""Customer-facing GST invoice PDF generation and business settings."""
from decimal import Decimal
from html import escape
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy.orm import Session, joinedload

from .. import auth, models, schemas
from ..database import get_db
from .reports import _NumberedCanvas, _PDF_FONT, _PDF_BOLD_FONT, _logo

router = APIRouter(tags=["Invoice Documents"])
GREEN = colors.HexColor("#1F5B3B")
PALE = colors.HexColor("#EEF4EB")


def _fmt(value):
    return f"₹{Decimal(str(value or 0)):,.2f}"


def _paragraph(text, style):
    return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), style)


def _invoice_pdf(invoice, settings):
    out = BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=12*mm, rightMargin=12*mm,
                            topMargin=11*mm, bottomMargin=17*mm, title=f"Invoice {invoice.invoice_no}")
    base = getSampleStyleSheet()
    small = ParagraphStyle("InvoiceSmall", parent=base["Normal"], fontName=_PDF_FONT, fontSize=7.2, leading=9)
    tiny = ParagraphStyle("InvoiceTiny", parent=small, fontSize=6.5, leading=8)
    title = ParagraphStyle("InvoiceTitle", parent=base["Heading1"], fontName=_PDF_BOLD_FONT,
                           fontSize=16, leading=20, textColor=GREEN, spaceAfter=3)
    section = ParagraphStyle("InvoiceSection", parent=base["Heading3"], fontName=_PDF_BOLD_FONT,
                             fontSize=8, leading=10, textColor=GREEN, spaceBefore=4, spaceAfter=3)
    story = []
    brand = Table([[_logo(), Paragraph(escape(settings.business_name), title),
                    Paragraph(f"<b>{escape(invoice.invoice_type or 'TAX INVOICE')}</b><br/>Invoice No: {escape(invoice.invoice_no)}<br/>Date: {invoice.date:%d %b %Y}", small)]],
                  colWidths=[20*mm, 95*mm, 65*mm])
    brand.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("ALIGN",(2,0),(2,0),"RIGHT"),
                               ("LINEBELOW",(0,0),(-1,-1),1,GREEN),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
    story += [brand, Spacer(1, 3*mm)]
    seller = f"{settings.address}<br/>GSTIN/UIN: {settings.gstin}<br/>State: {settings.state_name}, Code: {settings.state_code}<br/>Contact: {settings.phone or '—'}"
    buyer_name = invoice.customer_name or (invoice.farmer.name if invoice.farmer else "Walk-in Customer")
    buyer_addr = invoice.customer_address or (invoice.farmer.delivery_address or invoice.farmer.billing_address or invoice.farmer.location if invoice.farmer else "")
    buyer_gstin = invoice.customer_gstin or (invoice.farmer.gstin if invoice.farmer else None)
    buyer_phone = invoice.customer_phone or (invoice.farmer.mobile if invoice.farmer else None)
    buyer_state = invoice.customer_state_name or (invoice.farmer.state_name if invoice.farmer else None)
    buyer_code = invoice.customer_state_code or (invoice.farmer.state_code if invoice.farmer else None)
    meta = Table([[Paragraph(f"<b>SUPPLIER</b><br/>{seller}", small),
                   Paragraph(f"<b>BILL TO</b><br/><b>{escape(buyer_name)}</b><br/>{escape(buyer_addr or '')}<br/>GSTIN/UIN: {escape(buyer_gstin or 'Unregistered')}<br/>State: {escape(buyer_state or '—')}, Code: {escape(buyer_code or '—')}<br/>Contact: {escape(buyer_phone or '—')}", small)]],
                  colWidths=[88*mm, 92*mm])
    meta.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),PALE),("BOX",(0,0),(-1,-1),.4,colors.HexColor("#B7C9B4")),
                              ("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),
                              ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
    story += [meta, Spacer(1, 3*mm)]
    details = [["Place of Supply", f"{invoice.place_of_supply_state_name or buyer_state or settings.state_name} ({invoice.place_of_supply_state_code or buyer_code or settings.state_code})",
                "Payment", (invoice.payment_method.value if invoice.payment_method else "").upper(), "Due Date", invoice.due_date.strftime("%d %b %Y") if invoice.due_date else "—"]]
    info = Table(details, colWidths=[23*mm, 47*mm, 20*mm, 29*mm, 18*mm, 43*mm])
    info.setStyle(TableStyle([("FONTNAME",(0,0),(-1,-1),_PDF_FONT),("FONTSIZE",(0,0),(-1,-1),6.6),
                              ("BACKGROUND",(0,0),(-1,-1),colors.white),("GRID",(0,0),(-1,-1),.3,colors.HexColor("#CBD5C8")),
                              ("VALIGN",(0,0),(-1,-1),"MIDDLE"),("PADDING",(0,0),(-1,-1),4)]))
    story += [info, Spacer(1, 3*mm)]

    heads = ["No.", "Product Name", "HSN/SAC", "Quantity", "Rate Incl. Tax", "Rate Excl. Tax",
             "Discount", "Taxable Value", "GST Rate", "GST Amount", "Total Amount"]
    rows = [heads]
    for idx, item in enumerate(invoice.items, 1):
        gst = Decimal(str(item.gst_rate or 0))
        listed = Decimal(str(item.rate or 0))
        if (item.selling_price_tax_mode or "exclusive") == "inclusive":
            rate_incl = listed
            rate_excl = listed / (1 + gst / 100) if gst else listed
        else:
            rate_excl = listed
            rate_incl = listed * (1 + gst / 100)
        desc = _paragraph(item.product_name, tiny)
        taxable = Decimal(str(item.taxable_amount)) if item.taxable_amount is not None else None
        tax_amount = Decimal(str(item.tax_amount)) if item.tax_amount is not None else None
        discount = Decimal(str(item.discount_amount or 0))
        rows.append([str(idx), desc, item.hsn_code or "—", f"{Decimal(item.qty):,.2f}", _fmt(rate_incl), _fmt(rate_excl),
                     _fmt(discount), _fmt(taxable) if taxable is not None else "—",
                     f"{gst}%" if item.gst_rate is not None else "—",
                     _fmt(tax_amount) if tax_amount is not None else "—",
                     _fmt(item.total_amount if item.total_amount is not None else item.amount)])
    table = Table(rows, colWidths=[6*mm, 31*mm, 14*mm, 13*mm, 17*mm, 17*mm, 14*mm, 17*mm, 9*mm, 14*mm, 18*mm], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),GREEN),("TEXTCOLOR",(0,0),(-1,0),colors.white),
                               ("FONTNAME",(0,0),(-1,0),_PDF_BOLD_FONT),("FONTNAME",(0,1),(-1,-1),_PDF_FONT),
                               ("FONTSIZE",(0,0),(-1,0),6.2),("FONTSIZE",(0,1),(-1,-1),6.2),
                               ("GRID",(0,0),(-1,-1),.3,colors.HexColor("#B7C9B4")),
                               ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,PALE]),
                               ("VALIGN",(0,0),(-1,-1),"MIDDLE"),("ALIGN",(2,1),(-1,-1),"RIGHT"),
                               ("ALIGN",(0,0),(0,-1),"CENTER"),("TOPPADDING",(0,0),(-1,-1),4),
                               ("BOTTOMPADDING",(0,0),(-1,-1),4)]))
    story += [table, Spacer(1, 3*mm)]

    summary = [["Taxable Value", "CGST OUTPUT TAX", "SGST OUTPUT TAX", "IGST OUTPUT TAX", "Total Tax"],
               [_fmt(invoice.subtotal - invoice.discount), _fmt(invoice.cgst_amount), _fmt(invoice.sgst_amount), _fmt(invoice.igst_amount), _fmt(invoice.tax)]]
    tax_table = Table(summary, colWidths=[37*mm, 36*mm, 36*mm, 36*mm, 36*mm])
    tax_table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),PALE),("FONTNAME",(0,0),(-1,-1),_PDF_FONT),
                                   ("FONTNAME",(0,0),(-1,0),_PDF_BOLD_FONT),("FONTSIZE",(0,0),(-1,-1),6.8),
                                   ("GRID",(0,0),(-1,-1),.3,colors.HexColor("#B7C9B4")),("ALIGN",(0,0),(-1,-1),"RIGHT"),
                                   ("PADDING",(0,0),(-1,-1),4)]))
    story += [Paragraph("GST SUMMARY", section), tax_table, Spacer(1, 2*mm)]

    hsn_groups = {}
    for item in invoice.items:
        key = (item.hsn_code or "—", Decimal(str(item.gst_rate or 0)))
        group = hsn_groups.setdefault(key, {"taxable": Decimal("0"), "cgst": Decimal("0"), "sgst": Decimal("0"), "igst": Decimal("0")})
        group["taxable"] += Decimal(str(item.taxable_amount or 0))
        group["cgst"] += Decimal(str(item.cgst_amount or 0)); group["sgst"] += Decimal(str(item.sgst_amount or 0)); group["igst"] += Decimal(str(item.igst_amount or 0))
    hsn_rows = [["HSN/SAC", "Taxable Value", "CGST Rate", "CGST Amount", "SGST Rate", "SGST Amount", "IGST Rate", "IGST Amount", "Total Tax"]]
    for (hsn, gst), group in sorted(hsn_groups.items()):
        intra = Decimal(str(invoice.igst_amount or 0)) == 0
        hsn_rows.append([hsn, _fmt(group["taxable"]), f"{gst/2 if intra else 0}%", _fmt(group["cgst"]),
                         f"{gst/2 if intra else 0}%", _fmt(group["sgst"]), f"{0 if intra else gst}%", _fmt(group["igst"]),
                         _fmt(group["cgst"]+group["sgst"]+group["igst"])])
    hsn_table = Table(hsn_rows, colWidths=[18*mm,24*mm,16*mm,23*mm,16*mm,23*mm,16*mm,22*mm,22*mm], repeatRows=1)
    hsn_table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),GREEN),("TEXTCOLOR",(0,0),(-1,0),colors.white),
                                   ("FONTNAME",(0,0),(-1,0),_PDF_BOLD_FONT),("FONTNAME",(0,1),(-1,-1),_PDF_FONT),
                                   ("FONTSIZE",(0,0),(-1,-1),5.8),("GRID",(0,0),(-1,-1),.3,colors.HexColor("#B7C9B4")),
                                   ("ALIGN",(1,1),(-1,-1),"RIGHT"),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("PADDING",(0,0),(-1,-1),3)]))
    story += [Paragraph("HSN/SAC-WISE TAX SUMMARY", section), hsn_table, Spacer(1, 3*mm)]

    total_rows = [["Subtotal", _fmt(invoice.subtotal)], ["Less: Discount", f"− {_fmt(invoice.discount)}"],
                  ["CGST Output Tax", _fmt(invoice.cgst_amount)], ["SGST Output Tax", _fmt(invoice.sgst_amount)],
                  ["IGST Output Tax", _fmt(invoice.igst_amount)], ["Less/Add: Rounding", _fmt(invoice.rounding_adjustment)],
                  ["FINAL INVOICE TOTAL", _fmt(invoice.grand_total)], ["Amount Chargeable in Words", invoice.amount_in_words or ""]]
    totals = Table(total_rows, colWidths=[70*mm, 45*mm], hAlign="RIGHT")
    totals.setStyle(TableStyle([("FONTNAME",(0,0),(-1,-1),_PDF_FONT),("FONTSIZE",(0,0),(-1,-1),7.5),
                                ("ALIGN",(1,0),(1,-1),"RIGHT"),("LINEABOVE",(0,6),(-1,6),1,GREEN),
                                ("FONTNAME",(0,6),(-1,6),_PDF_BOLD_FONT),("FONTSIZE",(0,6),(-1,6),10),
                                ("SPAN",(1,7),(1,7)),("BACKGROUND",(0,6),(-1,6),PALE),("PADDING",(0,0),(-1,-1),4)]))
    story += [totals, Spacer(1, 3*mm)]

    if invoice.irn or invoice.acknowledgement_number or invoice.eway_bill_number:
        electronic = []
        if invoice.irn: electronic.append(["IRN", invoice.irn])
        if invoice.acknowledgement_number: electronic.append(["Acknowledgement No.", invoice.acknowledgement_number])
        if invoice.acknowledgement_date: electronic.append(["Acknowledgement Date", invoice.acknowledgement_date.strftime("%d %b %Y")])
        if invoice.eway_bill_number: electronic.append(["e-Way Bill No.", invoice.eway_bill_number])
        story += [Table(electronic, colWidths=[45*mm, 130*mm], style=TableStyle([("FONTNAME",(0,0),(-1,-1),_PDF_FONT),("FONTSIZE",(0,0),(-1,-1),7),("GRID",(0,0),(-1,-1),.3,colors.lightgrey),("PADDING",(0,0),(-1,-1),4)])), Spacer(1, 3*mm)]
    if settings.bank_name or settings.account_number:
        bank_text = f"<b>Bank Details</b><br/>Bank: {escape(settings.bank_name or '')}<br/>Account Holder: {escape(settings.account_holder_name or '')}<br/>Account No.: {escape(settings.account_number or '')}<br/>Branch: {escape(settings.branch or '')}<br/>IFSC: {escape(settings.ifsc_code or '')}"
        story.append(Paragraph(bank_text, small))
    if invoice.payment_terms:
        story.append(Paragraph(f"<b>Payment Terms:</b> {escape(invoice.payment_terms)}", small))
    if invoice.remarks:
        story.append(Paragraph(f"<b>Remarks:</b> {escape(invoice.remarks)}", small))
    if settings.declaration:
        story.append(Paragraph("DECLARATION", section)); story.append(Paragraph(escape(settings.declaration), small))
    sig = Table([[Paragraph("Customer's Seal and Signature", small), Paragraph(f"For {escape(settings.business_name)}<br/><br/><br/><b>Authorised Signatory</b>", small)],
                 [Paragraph("Computer Generated Invoice", tiny), Paragraph("", small)]], colWidths=[90*mm, 90*mm])
    sig.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"BOTTOM"),("ALIGN",(1,0),(1,-1),"RIGHT"),
                             ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),4)]))
    story += [Spacer(1, 4*mm), sig]
    doc.build(story, canvasmaker=_NumberedCanvas)
    return out.getvalue().rstrip(b"\r\n")


@router.get("/invoices/{invoice_id}/pdf")
def download_invoice_pdf(invoice_id: int, db: Session = Depends(get_db),
                         current_user: models.User = Depends(auth.get_current_user)):
    invoice = (db.query(models.Invoice).options(joinedload(models.Invoice.items), joinedload(models.Invoice.farmer))
               .filter(models.Invoice.id == invoice_id).first())
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    settings = db.query(models.BusinessSettings).filter(models.BusinessSettings.id == 1).first()
    if not settings:
        raise HTTPException(status_code=500, detail="Business settings are not configured")
    pdf = _invoice_pdf(invoice, settings)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{invoice.invoice_no}.pdf"'})


@router.get("/business-settings", response_model=schemas.BusinessSettingsOut)
def get_business_settings(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    settings = db.query(models.BusinessSettings).filter(models.BusinessSettings.id == 1).first()
    if not settings:
        raise HTTPException(status_code=404, detail="Business settings are not configured")
    return settings


@router.put("/business-settings", response_model=schemas.BusinessSettingsOut)
def update_business_settings(payload: schemas.BusinessSettingsUpdate, db: Session = Depends(get_db),
                             current_user: models.User = Depends(auth.require_roles("admin"))):
    settings = db.query(models.BusinessSettings).filter(models.BusinessSettings.id == 1).first()
    if not settings:
        settings = models.BusinessSettings(id=1)
        db.add(settings)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(settings, key, value)
    db.commit()
    db.refresh(settings)
    return settings
