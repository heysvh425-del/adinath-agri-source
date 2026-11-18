"""Professional, data-backed report exports for Adinath Agri Services."""
from datetime import UTC, datetime, timedelta, date, time
from decimal import Decimal
from pathlib import Path
from typing import List
import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy.orm import Session, joinedload
from .. import auth, models, schemas
from ..database import get_db

router = APIRouter(prefix="/reports", tags=["Reports"])
BUSINESS = {"name": "ADINATH AGRI SERVICES", "tagline": "Smart Farming. Trusted Service.", "gstin": "29ACPFA8181K1ZM", "phone": "8431271008", "address": "SH-72, Ugar Budruk, Kusnal Road, Karnataka - 591316"}
GREEN, PALE, GOLD = "1F5B3B", "EAF4E5", "D8B15A"
# Use a Unicode font when the host provides one so the Indian rupee symbol is
# rendered as ₹ (rather than a substituted glyph in the built-in PDF fonts).
# PDFs never use a base-14 font for report text: those fonts do not include
# U+20B9 (₹). Noto Sans is bundled with the application and both faces are
# registered and embedded. The family mapping prevents ReportLab from sending
# <b> text and table headers back to Helvetica-Bold.
_FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
_FONT_CHOICES = (
    (_FONT_DIR / "NotoSans-Regular.ttf", _FONT_DIR / "NotoSans-Bold.ttf"),
    (Path("C:/Windows/Fonts/NotoSans-Regular.ttf"), Path("C:/Windows/Fonts/NotoSans-Bold.ttf")),
    (Path("C:/Windows/Fonts/DejaVuSans.ttf"), Path("C:/Windows/Fonts/DejaVuSans-Bold.ttf")),
    (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
)
for _regular_font, _bold_font in _FONT_CHOICES:
    if _regular_font.exists() and _bold_font.exists():
        pdfmetrics.registerFont(TTFont("AdinathReport", str(_regular_font)))
        pdfmetrics.registerFont(TTFont("AdinathReport-Bold", str(_bold_font)))
        pdfmetrics.registerFontFamily("AdinathReport", normal="AdinathReport", bold="AdinathReport-Bold", italic="AdinathReport", boldItalic="AdinathReport-Bold")
        break
else:
    raise RuntimeError("A Unicode font with Indian Rupee support is required for PDF reports.")
_PDF_FONT = "AdinathReport"
_PDF_BOLD_FONT = "AdinathReport-Bold"

class _NumberedCanvas(canvas.Canvas):
    """Adds a final Page X of Y footer after ReportLab knows the page count."""
    def __init__(self, *args, **kwargs):
        canvas.Canvas.__init__(self, *args, **kwargs); self._pages = []
    def showPage(self):
        self._pages.append(dict(self.__dict__)); self._startPage()
    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self.setFillColor(colors.white); self.rect(165*mm, 5*mm, 31*mm, 7*mm, stroke=0, fill=1)
            self.setFillColor(colors.HexColor("#526B5A")); self.setFont(_PDF_FONT, 7)
            self.drawRightString(195*mm, 7.5*mm, f"Page {self._pageNumber} of {total}")
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)

def _period_dates(period: str, start_date: date | None = None, end_date: date | None = None) -> tuple[datetime, datetime]:
    now = datetime.now(UTC).replace(tzinfo=None); today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "custom":
        if not start_date or not end_date: raise HTTPException(422, "Custom reports require both start_date and end_date")
        if end_date < start_date: raise HTTPException(422, "End date cannot be before start date")
        return datetime.combine(start_date, time.min), datetime.combine(end_date, time.max)
    return (today, now) if period == "today" else ((today - timedelta(days=today.weekday()), now) if period == "weekly" else (today.replace(day=1), now))

def _money(value) -> str: return f"₹{Decimal(str(value)):,.2f}"
def _filename(period, extension): return f"Adinath_Agri_{period.title()}_Report.{extension}"
def _range_text(data): return data["start"].strftime("%d %B %Y") if data["period"] == "today" else f"{data['start']:%d %b %Y} – {data['end']:%d %b %Y}"

def _validate_report(data: dict) -> None:
    """Reject inconsistent reports rather than silently exporting wrong totals."""
    if len(data["transactions"]) != len(data["invoices"]): raise RuntimeError("Report reconciliation failed: duplicate or missing invoice")
    if sum((row["amount"] for row in data["transactions"]), Decimal("0")) != data["sales"]: raise RuntimeError("Report reconciliation failed: transaction sales mismatch")
    if sum((item["sales"] for item in data["products"].values()), Decimal("0")) != data["sales"]: raise RuntimeError("Report reconciliation failed: product sales mismatch")
    if sum(data["payments"].values(), Decimal("0")) != data["paid"]: raise RuntimeError("Report reconciliation failed: payment totals mismatch")
    if data["profit"] != data["sales"] - data["purchases"] - data["expenses"]: raise RuntimeError("Report reconciliation failed: profit mismatch")

def _report_data(period: str, db: Session, start_date: date | None = None, end_date: date | None = None) -> dict:
    start, end = _period_dates(period, start_date, end_date)
    invoices = (db.query(models.Invoice).options(joinedload(models.Invoice.farmer), joinedload(models.Invoice.items))
                .filter(models.Invoice.date >= start, models.Invoice.date <= end).order_by(models.Invoice.date.desc()).all())
    sales = sum((Decimal(i.grand_total) for i in invoices), Decimal("0")); paid = sum((Decimal(i.amount_paid) for i in invoices), Decimal("0")); due = sum((Decimal(i.balance_due) for i in invoices), Decimal("0"))
    products, payments, transactions = {}, {}, []
    for invoice in invoices:
        customer, method = (invoice.farmer.name if invoice.farmer else "Walk-in Customer"), invoice.payment_method.value.title()
        payments[method] = payments.get(method, Decimal("0")) + Decimal(invoice.amount_paid)
        item_total = sum((Decimal(item.amount) for item in invoice.items), Decimal("0")); allocated = Decimal("0")
        for index, item in enumerate(invoice.items):
            share = (Decimal(invoice.grand_total) - allocated) if index == len(invoice.items) - 1 else (Decimal(invoice.grand_total) * Decimal(item.amount) / item_total).quantize(Decimal("0.01")) if item_total else Decimal("0")
            allocated += share
            product = products.setdefault(item.product_name, {"quantity": Decimal("0"), "sales": Decimal("0")}); product["quantity"] += Decimal(item.qty); product["sales"] += share
        transactions.append({"date":invoice.date,"invoice":invoice.invoice_no,"customer":customer,"product":", ".join(item.product_name for item in invoice.items) or "-","quantity":sum((Decimal(item.qty) for item in invoice.items), Decimal("0")),"amount":Decimal(invoice.grand_total),"payment":method,"status":invoice.status.value.title()})
    purchase_entries = (db.query(models.PurchaseEntry)
                        .filter(models.PurchaseEntry.purchase_date >= start,
                                models.PurchaseEntry.purchase_date <= end).all())
    purchases = sum((Decimal(row.total_amount) for row in purchase_entries), Decimal("0"))
    # Expense tracking is not represented in the existing schema.
    expenses = Decimal("0")
    data = {"period":period,"start":start,"end":end,"invoices":invoices,"transactions":transactions,"products":products,"sales":sales,"paid":paid,"due":due,"purchases":purchases,"expenses":expenses,"profit":sales-purchases-expenses,"payments":payments}
    _validate_report(data)
    return data

def _logo():
    candidate = Path(__file__).resolve().parents[3] / "frontend" / "icons" / "icon-192.png"
    return Image(str(candidate), 16*mm, 16*mm) if candidate.exists() else ""

def _table(rows, widths, font_size=8, repeat=True, align_amounts=True):
    table = Table(rows, colWidths=widths, repeatRows=1 if repeat else 0)
    style = [("BACKGROUND",(0,0),(-1,0),colors.HexColor("#"+GREEN)), ("TEXTCOLOR",(0,0),(-1,0),colors.white), ("FONTNAME",(0,0),(-1,-1),_PDF_FONT), ("FONTNAME",(0,0),(-1,0),_PDF_BOLD_FONT), ("GRID",(0,0),(-1,-1),.3,colors.HexColor("#BDD3BA")), ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F4F8F2")]), ("FONTSIZE",(0,0),(-1,-1),font_size), ("VALIGN",(0,0),(-1,-1),"MIDDLE"), ("TOPPADDING",(0,0),(-1,-1),5), ("BOTTOMPADDING",(0,0),(-1,-1),5)]
    if align_amounts: style.append(("ALIGN",(1,1),(-1,-1),"RIGHT"))
    table.setStyle(TableStyle(style)); return table

def _pdf_bytes(data: dict) -> bytes:
    """Create a branded, paginated commercial report from live invoice data."""
    out = io.BytesIO(); doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=15*mm, rightMargin=15*mm, topMargin=14*mm, bottomMargin=17*mm)
    styles = getSampleStyleSheet(); title = ParagraphStyle("t", parent=styles["Normal"], fontName=_PDF_FONT, fontSize=19, leading=23, textColor=colors.HexColor("#"+GREEN)); sub = ParagraphStyle("s", parent=styles["Normal"], fontName=_PDF_FONT, fontSize=8, leading=11, textColor=colors.HexColor("#526B5A")); sec = ParagraphStyle("h", parent=styles["Heading2"], fontName=_PDF_FONT, fontSize=11, leading=15, textColor=colors.HexColor("#"+GREEN), spaceBefore=12, spaceAfter=6)
    story=[]
    head=Table([[_logo(),[Paragraph(BUSINESS["name"],title),Paragraph(BUSINESS["tagline"],sub)]]],colWidths=[20*mm,150*mm]); head.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"MIDDLE"),("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),0)])); story += [head,Spacer(1,4*mm)]
    meta=Table([[Paragraph(f"<b>BUSINESS REPORT</b><br/>Report Type: {data['period'].title()}<br/>Report Period: {_range_text(data)}",sub),Paragraph(f"<b>GSTIN:</b> {BUSINESS['gstin']}<br/><b>Phone:</b> {BUSINESS['phone']}<br/>{BUSINESS['address']}",sub),Paragraph(f"<b>Generated</b><br/>{datetime.now():%d %b %Y, %I:%M %p}",sub)]],colWidths=[55*mm,75*mm,40*mm]); meta.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#"+PALE)),("BOX",(0,0),(-1,-1),.4,colors.HexColor("#B7D1B4")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("PADDING",(0,0),(-1,-1),7)])); story += [meta,Spacer(1,5*mm)]
    kpis=[("TOTAL SALES",_money(data["sales"])),("TOTAL PURCHASES",_money(data["purchases"])),("TOTAL EXPENSES",_money(data["expenses"])),("TOTAL PROFIT",_money(data["profit"])),("TRANSACTIONS",str(len(data["invoices"])))]
    kpi=Table([[Paragraph(f'<font color="#597261"><b>{x}</b></font><br/><font color="#{GREEN}" size="11"><b>{y}</b></font>',sub) for x,y in kpis]],colWidths=[34*mm]*5); kpi.setStyle(TableStyle([("BOX",(0,0),(-1,-1),.4,colors.HexColor("#B7D1B4")),("INNERGRID",(0,0),(-1,-1),.3,colors.HexColor("#D6E5D3")),("PADDING",(0,0),(-1,-1),7)])); story.append(kpi)
    story += [Paragraph("Sales, Purchase, Expense & Profit Summary",sec),_table([["Business Metric","Amount"],["Sales Summary",_money(data["sales"])],["Purchase Summary",_money(data["purchases"])],["Expense Summary",_money(data["expenses"])],["Profit Summary",_money(data["profit"])],["Amount Collected",_money(data["paid"])],["Credit / Outstanding",_money(data["due"])]],[85*mm,85*mm])]
    payment=[["Payment / Cash / Credit Details","Amount Received"]]+[[name,_money(value)] for name,value in sorted(data["payments"].items())]
    if len(payment)==1: payment.append(["No payments recorded",_money(0)])
    story += [Paragraph("Payment / Cash / Credit Details",sec),_table(payment,[85*mm,85*mm])]
    products=[["Product","Quantity","Sales","Contribution"]]
    for name,item in sorted(data["products"].items(),key=lambda pair:pair[1]["sales"],reverse=True): products.append([name,f"{item['quantity']:,.2f}",_money(item["sales"]),f"{(item['sales']/data['sales']*100) if data['sales'] else 0:.1f}%"])
    if len(products)==1: products.append(["No product sales recorded","—",_money(0),"0.0%"])
    story += [Paragraph("Product-wise Sales & Top Products",sec),_table(products,[75*mm,27*mm,38*mm,30*mm])]
    tx=[["Date","Invoice","Customer","Product","Qty","Amount","Payment","Status"]]
    for item in data["transactions"]: tx.append([item["date"].strftime("%d %b %y"),item["invoice"],item["customer"],item["product"],f"{item['quantity']:,.2f}",_money(item["amount"]),item["payment"],item["status"]])
    if len(tx)==1: tx.append(["—","—","No transactions recorded for this period","—","—",_money(0),"—","—"])
    story += [Paragraph("Transaction Details",sec),_table(tx,[18*mm,22*mm,31*mm,34*mm,12*mm,24*mm,16*mm,15*mm],6.4)]
    def footer(canvas, document):
        canvas.saveState(); canvas.setStrokeColor(colors.HexColor("#B8D0B5")); canvas.line(15*mm,12*mm,195*mm,12*mm); canvas.setFont(_PDF_FONT,7); canvas.setFillColor(colors.HexColor("#526B5A")); canvas.drawString(15*mm,7.5*mm,"ADINATH AGRI SERVICES | Confidential Business Report | Generated automatically from the management system"); canvas.drawRightString(195*mm,7.5*mm,f"Page {document.page}"); canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer,canvasmaker=_NumberedCanvas)
    # ReportLab correctly terminates PDFs with a newline; keep the legacy API's
    # exact EOF contract while preserving a standards-compliant final marker.
    return out.getvalue().rstrip(b"\r\n")

def _style_data(ws, header, last, cols):
    dark,pale=PatternFill("solid",fgColor=GREEN),PatternFill("solid",fgColor=PALE); border=Border(*(Side(style="thin",color="B7D1B4") for _ in range(4)))
    for row in range(header,last+1):
        for col in range(1,cols+1):
            cell=ws.cell(row,col); cell.border=border; cell.alignment=Alignment(vertical="center")
            if row==header: cell.fill=dark; cell.font=Font(color="FFFFFF",bold=True); cell.alignment=Alignment(horizontal="center",vertical="center")
            elif row%2: cell.fill=pale

def _brand(ws, data, report_title, cols):
    ws.merge_cells(start_row=1,start_column=1,end_row=1,end_column=cols); ws.cell(1,1,BUSINESS["name"]).fill=PatternFill("solid",fgColor=GREEN); ws.cell(1,1).font=Font(color="FFFFFF",bold=True,size=16); ws.cell(1,1).alignment=Alignment(horizontal="center"); ws.row_dimensions[1].height=29
    ws.merge_cells(start_row=2,start_column=1,end_row=2,end_column=cols); ws.cell(2,1,f"{report_title}  |  {_range_text(data)}  |  Generated {datetime.now():%d %b %Y, %I:%M %p}").fill=PatternFill("solid",fgColor=PALE); ws.cell(2,1).font=Font(italic=True,color="48654E"); ws.cell(2,1).alignment=Alignment(horizontal="center"); ws.sheet_view.showGridLines=False

def _xlsx_bytes(data: dict) -> bytes:
    book=Workbook(); summary=book.active; summary.title="Report Summary"; tx=book.create_sheet("Transactions"); analysis=book.create_sheet("Product Sales Analysis"); currency='₹#,##0.00;[Red]-₹#,##0.00'; pale=PatternFill("solid",fgColor=PALE)
    _brand(summary,data,f"{data['period'].title()} Business Report",5); summary.merge_cells("A4:E4"); summary["A4"]=f"GSTIN: {BUSINESS['gstin']}  |  {BUSINESS['address']}"; summary["A4"].alignment=Alignment(horizontal="center"); summary["A4"].font=Font(color="48654E")
    metrics=[("TOTAL SALES",data["sales"]),("TOTAL PURCHASES",data["purchases"]),("TOTAL EXPENSES",data["expenses"]),("TOTAL PROFIT",data["profit"]),("TRANSACTIONS",len(data["invoices"]))]
    for col,(label,value) in enumerate(metrics,1):
        for row,val in ((6,label),(7,value)):
            cell=summary.cell(row,col,val); cell.fill=pale; cell.border=Border(*(Side(style="thin",color="B7D1B4") for _ in range(4))); cell.alignment=Alignment(horizontal="center"); cell.font=Font(bold=True,size=13 if row==7 else 9,color=GREEN if row==7 else "48654E")
        if col<5: summary.cell(7,col).number_format=currency
    summary["A10"]="Business Summary"; summary["A10"].font=Font(bold=True,size=12,color=GREEN); summary["D10"]="Payment Details"; summary["D10"].font=Font(bold=True,size=12,color=GREEN)
    for col,label in enumerate(["Metric","Amount"],1): summary.cell(11,col,label)
    rows=[("Sales Summary",data["sales"]),("Purchase Summary",data["purchases"]),("Expense Summary",data["expenses"]),("Profit Summary",data["profit"]),("Amount Collected",data["paid"]),("Credit / Outstanding",data["due"])]
    for row,(label,value) in enumerate(rows,12): summary.cell(row,1,label); summary.cell(row,2,value).number_format=currency
    payment=list(sorted(data["payments"].items())) or [("No payments recorded",Decimal("0"))]
    summary["D11"],summary["E11"]="Payment Type","Amount Received"
    for row,(label,value) in enumerate(payment,12): summary.cell(row,4,label); summary.cell(row,5,value).number_format=currency
    _style_data(summary,11,17,2); _style_data(summary,11,11+len(payment),5)
    for c,w in {"A":27,"B":18,"C":4,"D":24,"E":22}.items(): summary.column_dimensions[c].width=w
    summary.freeze_panes="A11"
    headers=["Date","Invoice / Transaction ID","Customer / Supplier","Product","Quantity","Amount","Payment Type","Status"]; _brand(tx,data,"Transaction Register",len(headers))
    for col,label in enumerate(headers,1): tx.cell(4,col,label)
    for row,item in enumerate(data["transactions"],5):
        for col,value in enumerate([item["date"],item["invoice"],item["customer"],item["product"],item["quantity"],item["amount"],item["payment"],item["status"]],1): tx.cell(row,col,value)
        tx.cell(row,1).number_format="dd mmm yyyy"; tx.cell(row,5).number_format='#,##0.00'; tx.cell(row,6).number_format=currency
    final=max(5,4+len(data["transactions"]));
    if not data["transactions"]: tx.merge_cells("A5:H5"); tx["A5"]="No transactions recorded for this reporting period."
    _style_data(tx,4,final,8); tx.auto_filter.ref=f"A4:H{final}"; tx.freeze_panes="A5"
    for col,width in enumerate([15,22,26,28,12,16,16,14],1): tx.column_dimensions[get_column_letter(col)].width=width
    _brand(analysis,data,"Product & Sales Analysis",4)
    for col,label in enumerate(["Product","Quantity","Sales","Percentage Contribution"],1): analysis.cell(4,col,label)
    products=sorted(data["products"].items(),key=lambda p:p[1]["sales"],reverse=True)
    for row,(name,item) in enumerate(products,5): analysis.cell(row,1,name); analysis.cell(row,2,item["quantity"]).number_format='#,##0.00'; analysis.cell(row,3,item["sales"]).number_format=currency; analysis.cell(row,4,(item["sales"]/data["sales"]) if data["sales"] else 0).number_format="0.0%"
    final=max(5,4+len(products));
    if not products: analysis.merge_cells("A5:D5"); analysis["A5"]="No product sales recorded for this reporting period."
    _style_data(analysis,4,final,4); analysis.auto_filter.ref=f"A4:D{final}"; analysis.freeze_panes="A5"
    for c,w in {"A":38,"B":16,"C":18,"D":26}.items(): analysis.column_dimensions[c].width=w
    output=io.BytesIO(); book.save(output); return output.getvalue()

def _public_report(data: dict) -> dict:
    return {"period": data["period"], "start": data["start"].date().isoformat(), "end": data["end"].date().isoformat(),
            "sales": float(data["sales"]), "purchases": float(data["purchases"]), "expenses": float(data["expenses"]), "profit": float(data["profit"]), "paid": float(data["paid"]), "due": float(data["due"]), "transactions": len(data["transactions"]),
            "payments": {name: float(value) for name, value in data["payments"].items()},
            "products": [{"name": name, "quantity": float(item["quantity"]), "sales": float(item["sales"]), "contribution": round(float(item["sales"] / data["sales"] * 100), 2) if data["sales"] else 0, "average_price": round(float(item["sales"] / item["quantity"]), 2) if item["quantity"] else 0} for name, item in sorted(data["products"].items(), key=lambda entry: entry[1]["sales"], reverse=True)],
            "rows": [{**row, "date": row["date"].isoformat(), "quantity": float(row["quantity"]), "amount": float(row["amount"])} for row in data["transactions"]]}

@router.get("/data")
def report_data(range: str=Query("today",pattern="^(today|weekly|monthly|custom)$"), start_date: date | None=Query(None), end_date: date | None=Query(None), db: Session=Depends(get_db), current_user: models.User=Depends(auth.get_current_user)):
    """The sole source for the Reports UI and its selected-period numbers."""
    return _public_report(_report_data(range, db, start_date, end_date))

@router.get("/export")
def export_report(range: str=Query("weekly",pattern="^(today|weekly|monthly|custom)$"),format: str=Query("pdf",pattern="^(pdf|xlsx|csv)$"),start_date: date | None=Query(None),end_date: date | None=Query(None),db: Session=Depends(get_db),current_user: models.User=Depends(auth.get_current_user)):
    data=_report_data(range,db,start_date,end_date)
    if format=="pdf": return Response(_pdf_bytes(data),media_type="application/pdf",headers={"Content-Disposition":f'attachment; filename="{_filename(range,"pdf")}"'})
    if format=="xlsx": return Response(_xlsx_bytes(data),media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",headers={"Content-Disposition":f'attachment; filename="{_filename(range,"xlsx")}"'})
    # CSV stays intentionally tabular: one header plus the exact transaction
    # rows used by the dashboard and other export formats.
    output=io.StringIO(newline=""); writer=csv.writer(output)
    writer.writerow(["Date","Invoice No","Customer","Product","Quantity","Amount","Payment Method","Status"])
    for item in data["transactions"]: writer.writerow([item["date"].strftime("%Y-%m-%d %H:%M"),item["invoice"],item["customer"],item["product"],f"{item['quantity']:.2f}",_money(item["amount"]),item["payment"],item["status"]])
    return Response(content="\ufeff"+output.getvalue(),media_type="text/csv; charset=utf-8",headers={"Content-Disposition":f'attachment; filename="{_filename(range,"csv")}"'})

@router.get("/overview")
def reports_overview(db: Session=Depends(get_db),current_user: models.User=Depends(auth.get_current_user)):
    result={}
    for period in ("today","weekly","monthly"):
        data=_report_data(period,db); result[period]={"start":data["start"].date().isoformat(),"end":data["end"].date().isoformat(),"sales":float(data["sales"]),"purchases":float(data["purchases"]),"expenses":float(data["expenses"]),"profit":float(data["profit"]),"transactions":len(data["invoices"])}
    return result

@router.get("/top-products",response_model=List[schemas.TopProduct])
def top_products(limit: int=Query(5,ge=1,le=100),db: Session=Depends(get_db),current_user: models.User=Depends(auth.get_current_user)):
    totals={}
    for item in db.query(models.InvoiceItem).all(): totals[item.product_name]=totals.get(item.product_name,0)+item.amount
    return [schemas.TopProduct(name=name,total=round(total,2)) for name,total in sorted(totals.items(),key=lambda i:i[1],reverse=True)[:limit]]

@router.get("/payment-methods",response_model=List[schemas.PaymentSplit])
def payment_methods(db: Session=Depends(get_db),current_user: models.User=Depends(auth.get_current_user)):
    totals={}; grand=0.0
    for payment in db.query(models.Payment).all(): method=payment.method.value; totals[method]=totals.get(method,0)+float(payment.amount); grand+=float(payment.amount)
    return [schemas.PaymentSplit(method=method,total=round(total,2),percent=round(total/grand*100,1) if grand else 0) for method,total in totals.items()]

@router.get("/sales-performance",response_model=List[schemas.SalesPoint])
def sales_performance(db: Session=Depends(get_db),current_user: models.User=Depends(auth.get_current_user)):
    from .dashboard import sales_chart
    return sales_chart(range="7d",db=db,current_user=current_user)
