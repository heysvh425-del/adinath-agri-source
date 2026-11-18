"""End-to-end checks for the core billing rules."""
import os
import tempfile
import io
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
import pytest

# Configure an isolated SQLite file before importing the application modules.
db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
db_file.close()
os.environ["ADINATH_DB_PATH"] = db_file.name

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.database import Base, engine  # noqa: E402
from app.database.catalog import PRODUCTS  # noqa: E402


@pytest.fixture(autouse=True)
def reset_database():
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS app_migrations")
    Base.metadata.create_all(bind=engine)
    yield


def login_as_admin(client: TestClient) -> dict[str, str]:
    setup = client.post("/auth/setup", json={
        "name": "Test Admin", "username": "admin", "password": "safe-password-123",
    })
    assert setup.status_code == 201
    login = client.post("/auth/login", data={"username": "admin", "password": "safe-password-123"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_invoice_deducts_stock_and_updates_dashboard():
    client = TestClient(app)
    headers = login_as_admin(client)
    product = client.post("/products", headers=headers, json={
        "name": "Seed & Feed", "sku": "SEED-1", "category": "Fertilizer", "unit": "Bag",
        "rate": 100, "stock": 10, "min_stock": 2,
    })
    assert product.status_code == 201

    invoice = client.post("/invoices", headers=headers, json={
        "items": [{"product_id": product.json()["id"], "qty": 3}],
        "payment_method": "cash", "amount_paid": 315,
    })
    assert invoice.status_code == 201
    assert invoice.json()["grand_total"] == 315
    assert invoice.json()["status"] == "paid"

    remaining = client.get(f"/products/{product.json()['id']}", headers=headers)
    assert remaining.json()["stock"] == 7
    assert client.get("/dashboard/sales-chart?range=7d", headers=headers).status_code == 200


def test_custom_decimal_gst_is_saved_and_snapshotted_on_invoice():
    client = TestClient(app)
    headers = login_as_admin(client)
    product = client.post("/products", headers=headers, json={
        "name": "Custom GST Item", "sku": "CUSTOM-GST-1", "category": "Fertilizer",
        "unit": "Bag", "selling_price": 107.50, "stock": 2,
        "gst_rate": 7.5, "gst_selection_type": "custom", "selling_price_tax_mode": "inclusive",
    })
    assert product.status_code == 201, product.text
    assert float(product.json()["gst_rate"]) == 7.5
    assert product.json()["gst_selection_type"] == "custom"

    invoice = client.post("/invoices", headers=headers, json={
        "items": [{"product_id": product.json()["id"], "qty": 1}],
        "payment_method": "cash", "amount_paid": 107.50,
    })
    assert invoice.status_code == 201, invoice.text
    item = invoice.json()["items"][0]
    assert float(item["gst_rate"]) == 7.5
    assert item["gst_selection_type"] == "custom"
    assert item["selling_price_tax_mode"] == "inclusive"
    assert item["taxable_amount"] == 100
    assert item["tax_amount"] == 7.5
    assert item["total_amount"] == 107.5


def test_custom_gst_range_and_preset_rates_are_validated():
    client = TestClient(app)
    headers = login_as_admin(client)
    payload = {
        "name": "Invalid GST Item", "sku": "INVALID-GST-1", "category": "Fertilizer",
        "unit": "Bag", "selling_price": 10, "stock": 1,
    }
    too_high = client.post("/products", headers=headers, json={
        **payload, "gst_rate": 100.01, "gst_selection_type": "custom",
    })
    assert too_high.status_code == 422
    wrong_preset = client.post("/products", headers=headers, json={
        **payload, "sku": "INVALID-GST-2", "gst_rate": 7.5, "gst_selection_type": "preset",
    })
    assert wrong_preset.status_code == 422


def test_product_supplier_and_active_status_are_saved():
    client = TestClient(app)
    headers = login_as_admin(client)
    created = client.post("/products", headers=headers, json={
        "name": "Supplier Tracked Fertilizer", "sku": "SUP-1", "category": "Fertilizer",
        "unit": "Bag", "rate": 100, "stock": 10, "min_stock": 2,
        "supplier": "Adinath Wholesale",
    })
    assert created.status_code == 201
    assert created.json()["supplier"] == "Adinath Wholesale"
    listed = client.get("/products", headers=headers)
    assert listed.status_code == 200
    assert any(item["id"] == created.json()["id"] for item in listed.json())

    updated = client.put(f"/products/{created.json()['id']}", headers=headers, json={
        "supplier": "New Agri Supplier", "is_active": False,
    })
    assert updated.status_code == 200
    assert updated.json()["supplier"] == "New Agri Supplier"
    assert updated.json()["is_active"] is False


def test_product_description_and_buying_selling_prices_edit_independently():
    with TestClient(app) as client:
        headers = login_as_admin(client)
        created = client.post("/products", headers=headers, json={
            "name": "Independent Price Product", "description": "Description from the product master",
            "catalog_serial": 901, "sku": "INDEPENDENT-PRICE-1", "category": "Fertilizers",
            "unit": "Bag", "purchase_price_including_gst": 72.35, "selling_price": 100.50,
            "gst_rate": 5, "stock": 4,
        })
        assert created.status_code == 201, created.text
        changed = client.put(f"/products/{created.json()['id']}", headers=headers, json={
            "description": "Updated product master description", "purchase_price_including_gst": 73.25,
        })
        assert changed.status_code == 200, changed.text
        assert changed.json()["description"] == "Updated product master description"
        assert float(changed.json()["selling_price"]) == 100.50
        public = client.get(f"/products/{created.json()['id']}", headers=headers)
        assert "purchase_price_including_gst" not in public.json()
        internal = next(row for row in client.get("/products/internal", headers=headers).json()
                        if row["id"] == created.json()["id"])
        assert float(internal["purchase_price_including_gst"]) == 73.25
        assert float(internal["selling_price"]) == 100.50


def test_duplicate_line_items_cannot_bypass_stock_check():
    client = TestClient(app)
    headers = login_as_admin(client)
    product = client.post("/products", headers=headers, json={
        "name": "Fertilizer", "sku": "FERT-1", "category": "Fertilizer", "unit": "Bag",
        "rate": 10, "stock": 5, "min_stock": 1,
    }).json()

    invoice = client.post("/invoices", headers=headers, json={
        "items": [{"product_id": product["id"], "qty": 3}, {"product_id": product["id"], "qty": 3}],
        "payment_method": "cash", "amount_paid": 0,
    })
    assert invoice.status_code == 400
    assert "Insufficient stock" in invoice.json()["detail"]


def test_checkout_idempotency_key_prevents_duplicate_sale_after_retry():
    """A lost client response can safely be retried without deducting stock twice."""
    client = TestClient(app)
    headers = login_as_admin(client)
    product = client.post("/products", headers=headers, json={
        "name": "Retry-safe Seed", "sku": "SAFE-1", "category": "Fertilizer",
        "unit": "Bag", "rate": 100, "stock": 5, "min_stock": 1,
    }).json()
    checkout_headers = {**headers, "X-Idempotency-Key": "checkout-retry-001"}
    payload = {
        "items": [{"product_id": product["id"], "qty": 2}],
        "payment_method": "cash", "amount_paid": 210,
    }

    first = client.post("/invoices", headers=checkout_headers, json=payload)
    retry = client.post("/invoices", headers=checkout_headers, json=payload)

    assert first.status_code == retry.status_code == 201
    assert first.json()["id"] == retry.json()["id"]
    assert client.get(f"/products/{product['id']}", headers=headers).json()["stock"] == 3


def test_checkout_idempotency_key_rejects_a_changed_bill():
    client = TestClient(app)
    headers = login_as_admin(client)
    product = client.post("/products", headers=headers, json={
        "name": "Changed Retry Seed", "sku": "CHANGED-1", "category": "Fertilizer",
        "unit": "Bag", "rate": 100, "stock": 5, "min_stock": 1,
    }).json()
    checkout_headers = {**headers, "X-Idempotency-Key": "checkout-changed-001"}
    original_payload = {
        "items": [{"product_id": product["id"], "qty": 2}],
        "payment_method": "cash", "amount_paid": 210,
    }

    first = client.post("/invoices", headers=checkout_headers, json=original_payload)
    changed_payload = {**original_payload, "items": [{"product_id": product["id"], "qty": 3}]}
    retry = client.post("/invoices", headers=checkout_headers, json=changed_payload)

    assert first.status_code == 201
    assert retry.status_code == 409
    assert client.get(f"/products/{product['id']}", headers=headers).json()["stock"] == 3


def test_svg_brand_asset_has_the_correct_content_type():
    response = TestClient(app).get("/icons/adinath-agri-logo.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")


def test_consecutive_invoices_continue_after_a_failed_attempt():
    """The invoice endpoint remains usable for Bill 1 -> Bill 2 -> Bill 3.

    A rejected checkout must roll back its transaction fully, so a POS client
    can immediately retry with a valid quantity without corrupting stock or
    the invoice sequence.
    """
    client = TestClient(app)
    headers = login_as_admin(client)
    product = client.post("/products", headers=headers, json={
        "name": "Consecutive Sale Product", "sku": "POS-3", "category": "Fertilizer",
        "unit": "Bag", "rate": 100, "stock": 10, "min_stock": 1,
    }).json()

    failed = client.post("/invoices", headers=headers, json={
        "items": [{"product_id": product["id"], "qty": 11}],
        "payment_method": "cash", "amount_paid": 0,
    })
    assert failed.status_code == 400

    invoice_numbers = []
    for quantity in (1, 2, 3):
        response = client.post("/invoices", headers=headers, json={
            "items": [{"product_id": product["id"], "qty": quantity}],
            "payment_method": "cash", "amount_paid": quantity * 105,
        })
        assert response.status_code == 201
        invoice_numbers.append(response.json()["invoice_no"])

    assert invoice_numbers == ["INV-000001", "INV-000002", "INV-000003"]
    assert client.get(f"/products/{product['id']}", headers=headers).json()["stock"] == 4


def test_invoice_caps_discount_and_payment_and_report_values_reconcile():
    client = TestClient(app)
    headers = login_as_admin(client)
    product = client.post("/products", headers=headers, json={
        "name": "Crop Guard", "sku": "CROP-1", "category": "Pesticides", "unit": "Bottle",
        "rate": 100, "stock": 5, "min_stock": 1,
    }).json()
    # A UI or API client cannot turn an oversized discount or payment into an
    # invalid stored total. The API and export data remain reconciled.
    invoice = client.post("/invoices", headers=headers, json={
        "items": [{"product_id": product["id"], "qty": 2}], "discount": 999,
        "payment_method": "cash", "amount_paid": 999,
    })
    assert invoice.status_code == 201
    data = invoice.json()
    assert data["subtotal"] == 200
    assert data["discount"] == 200
    assert data["tax"] == 0
    assert data["grand_total"] == 0
    assert data["amount_paid"] == 0
    assert data["balance_due"] == 0

    report = client.get("/reports/data?range=today", headers=headers)
    assert report.status_code == 200
    assert report.json()["sales"] == data["grand_total"]
    assert report.json()["rows"][0]["amount"] == data["grand_total"]


@pytest.mark.parametrize("period", ["today", "weekly", "monthly"])
def test_report_export_downloads_valid_pdf_for_every_period(period):
    client = TestClient(app)
    headers = login_as_admin(client)

    response = client.get(f"/reports/export?range={period}&format=pdf", headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert f'filename="Adinath_Agri_{period.title()}_Report.pdf"' in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF-1.4")
    assert response.content.endswith(b"%%EOF")


def test_report_export_downloads_a_valid_excel_workbook():
    client = TestClient(app)
    response = client.get("/reports/export?range=monthly&format=xlsx", headers=login_as_admin(client))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert response.content.startswith(b"PK")
    with zipfile.ZipFile(io.BytesIO(response.content)) as workbook:
        assert "xl/workbook.xml" in workbook.namelist()
        assert "xl/worksheets/sheet1.xml" in workbook.namelist()


def test_gst_invoice_pdf_cost_privacy_catalog_and_ca_export_workflow():
    """Exercise the imported catalog, customer tax data, PDF and internal exports together."""
    from pypdf import PdfReader

    with TestClient(app) as client:
        headers = login_as_admin(client)
        catalog = client.get("/products", headers=headers).json()
        assert len([p for p in catalog if p["sku"].startswith("AAS-")]) == 110
        assert not any("purchase_price_including_gst" in p for p in catalog)
        missing_sale_skus = {p["sku"] for p in catalog if p["sku"] in {"AAS-007", "AAS-008", "AAS-091"} and p["selling_price"] is None}
        assert missing_sale_skus == {"AAS-007", "AAS-008", "AAS-091"}

        registered = client.post("/farmers", headers=headers, json={
            "name": "GST Customer", "mobile": "9876543210", "billing_address": "12 Main Road",
            "delivery_address": "12 Main Road", "gstin": "29ABCDE1234F1Z5",
            "state_name": "Karnataka", "state_code": "29",
        })
        assert registered.status_code == 201, registered.text
        product = client.post("/products", headers=headers, json={
            "name": "Invoice Test Product", "sku": "GST-WORKFLOW-1", "category": "Fertilizer", "unit": "Bag",
            "selling_price": 100, "purchase_price_including_gst": 59, "gst_rate": 18,
            "gst_selection_type": "preset", "hsn_code": "38089390", "stock": 10,
        })
        assert product.status_code == 201, product.text

        local = client.post("/invoices", headers=headers, json={
            "farmer_id": registered.json()["id"], "items": [{"product_id": product.json()["id"], "qty": 1, "rate": 100, "discount_amount": 10}],
            "discount": 5, "payment_method": "cash", "amount_paid": 100,
            "due_date": "2026-12-01T00:00:00", "payment_terms": "Due in 14 days", "remarks": "Delivery confirmed",
        })
        assert local.status_code == 201, local.text
        local_data = local.json()
        assert local_data["customer_gstin"] == "29ABCDE1234F1Z5"
        assert local_data["place_of_supply_state_code"] == "29"
        assert local_data["items"][0]["hsn_code"] == "38089390"
        assert local_data["items"][0]["taxable_amount"] == 85
        assert local_data["cgst_amount"] == local_data["sgst_amount"] == 7.65
        assert local_data["igst_amount"] == 0
        assert local_data["rounding_adjustment"] == -0.3
        assert "purchase_rate_snapshot" not in local_data["items"][0]

        interstate = client.post("/invoices", headers=headers, json={
            "customer_name": "Unregistered Other State", "customer_state_name": "Goa", "customer_state_code": "30",
            "place_of_supply_state_code": "30", "items": [{"product_id": product.json()["id"], "qty": 1}],
            "payment_method": "credit", "amount_paid": 0,
        })
        assert interstate.status_code == 201, interstate.text
        assert interstate.json()["cgst_amount"] == interstate.json()["sgst_amount"] == 0
        assert interstate.json()["igst_amount"] == 18

        pdf = client.get(f"/invoices/{local_data['id']}/pdf", headers=headers)
        assert pdf.status_code == 200 and pdf.headers["content-type"].startswith("application/pdf")
        pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf.content)).pages)
        assert "TAX INVOICE" in pdf_text and "GST SUMMARY" in pdf_text
        assert "HSN/SAC-WISE TAX SUMMARY" in pdf_text and "Amount Chargeable in Words" in pdf_text
        assert "Buying Price" not in pdf_text and "Purchase Cost" not in pdf_text
        assert "Bank Details" not in pdf_text

        rows = client.get("/accounting/rows", headers=headers)
        assert rows.status_code == 200, rows.text
        ca_row = next(r for r in rows.json() if r["invoice_number"] == local_data["invoice_no"])
        assert ca_row["buying_price"] == 59
        assert ca_row["selling_price"] == 100
        assert ca_row["actual_purchase_rate"] == 50
        assert ca_row["purchase_value"] == 50

        report = client.get("/accounting/export?format=xlsx", headers=headers)
        assert report.status_code == 200 and report.content.startswith(b"PK")
        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(report.content), data_only=True)
        sheet = workbook["CA Accounting"]
        assert sheet.cell(3, 5).value == "Buying Price (Master, incl. GST)"
        assert [sheet.cell(4, col).value for col in range(5, 9)] == [59, 100, 50, 100]
        assert sheet.cell(3, 18).value == "Purchase Cost Snapshot Status"
        assert sheet.cell(4, 19).value == 15
        assert "Product Master" in workbook.sheetnames and "Purchase Transactions" in workbook.sheetnames
        staff = client.post("/auth/register", headers=headers, json={
            "name": "Shop Staff", "username": "staff_user", "password": "staff-password-123", "role": "staff",
        })
        assert staff.status_code == 201
        staff_login = client.post("/auth/login", data={"username": "staff_user", "password": "staff-password-123"})
        staff_headers = {"Authorization": f"Bearer {staff_login.json()['access_token']}"}
        assert client.get("/products/internal", headers=staff_headers).status_code == 403
        assert client.get("/accounting/rows", headers=staff_headers).status_code == 403

        accountant = client.post("/auth/register", headers=headers, json={
            "name": "CA User", "username": "ca_user", "password": "accounting-password-123", "role": "accountant",
        })
        assert accountant.status_code == 201, accountant.text
        ca_login = client.post("/auth/login", data={"username": "ca_user", "password": "accounting-password-123"})
        ca_headers = {"Authorization": f"Bearer {ca_login.json()['access_token']}"}
        assert client.get("/products/internal", headers=ca_headers).status_code == 200
        ca_pdf = client.get("/accounting/export?format=pdf", headers=ca_headers)
        assert ca_pdf.status_code == 200 and ca_pdf.content.startswith(b"%PDF-")
        ca_pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(ca_pdf.content)).pages)
        assert "Buying Price" in ca_pdf_text and "Purchase" in ca_pdf_text
        assert "<br/>" not in ca_pdf_text


def test_accounting_filters_and_audited_historical_cost_correction():
    with TestClient(app) as client:
        headers = login_as_admin(client)
        product = client.post("/products", headers=headers, json={
            "name": "Snapshot Review Product", "sku": "SNAPSHOT-REVIEW-1",
            "category": "Fertilizer", "unit": "Bag", "selling_price": 100,
            "stock": 2, "gst_rate": 18,
        })
        assert product.status_code == 201, product.text
        invoice = client.post("/invoices", headers=headers, json={
            "customer_name": "Snapshot Review Customer",
            "items": [{"product_id": product.json()["id"], "qty": 1}],
            "payment_method": "cash", "amount_paid": 118,
        })
        assert invoice.status_code == 201, invoice.text
        line_id = invoice.json()["items"][0]["id"]
        invoice_total_before = invoice.json()["grand_total"]

        missing = client.get("/accounting/rows?missing_cost_snapshot=missing", headers=headers)
        assert missing.status_code == 200, missing.text
        assert len(missing.json()) == 1
        assert missing.json()[0]["line_id"] == line_id
        assert missing.json()[0]["customer_name"] == "Snapshot Review Customer"
        assert client.get("/accounting/rows?invoice_search=does-not-exist", headers=headers).json() == []

        staff = client.post("/auth/register", headers=headers, json={
            "name": "Correction Staff", "username": "correction_staff",
            "password": "staff-password-123", "role": "staff",
        })
        assert staff.status_code == 201
        staff_login = client.post("/auth/login", data={"username": "correction_staff", "password": "staff-password-123"})
        staff_headers = {"Authorization": f"Bearer {staff_login.json()['access_token']}"}
        correction_payload = {
            "unit_cost_ex_tax": 62.50,
            "unit_cost_incl_gst": 73.75,
            "source_reference": "SUP-BILL-440",
            "reason": "Verified against supplier purchase bill",
        }
        assert client.post(f"/accounting/lines/{line_id}/cost-correction", headers=staff_headers, json=correction_payload).status_code == 403

        corrected = client.post(f"/accounting/lines/{line_id}/cost-correction", headers=headers, json=correction_payload)
        assert corrected.status_code == 200, corrected.text
        assert len(client.get("/accounting/rows?missing_cost_snapshot=missing", headers=headers).json()) == 0
        saved = client.get("/accounting/rows?missing_cost_snapshot=saved", headers=headers)
        assert saved.status_code == 200 and len(saved.json()) == 1
        assert saved.json()[0]["actual_purchase_rate"] == 62.5
        assert saved.json()[0]["purchase_value"] == 62.5

        invoice_after = client.get("/invoices", headers=headers).json()[0]
        assert invoice_after["grand_total"] == invoice_total_before
        internal_product = client.get("/products/internal", headers=headers).json()
        master_product = next(row for row in internal_product if row["id"] == product.json()["id"])
        assert master_product["purchase_price_including_gst"] is None
        with engine.connect() as connection:
            audit = connection.exec_driver_sql(
                "SELECT corrected_cost_ex_tax, source_reference, reason FROM invoice_cost_corrections WHERE invoice_item_id = ?",
                (line_id,),
            ).fetchone()
        assert audit == (62.5, "SUP-BILL-440", "Verified against supplier purchase bill")


def test_long_invoice_pdf_repeats_headers_and_numbers_pages():
    from pypdf import PdfReader

    with TestClient(app) as client:
        headers = login_as_admin(client)
        products = client.get("/products", headers=headers).json()
        selected = [p for p in products if p["sku"].startswith("AAS-") and p["selling_price"] is not None][:55]
        assert len(selected) == 55
        invoice = client.post("/invoices", headers=headers, json={
            "customer_name": "Multi-page PDF check", "items": [{"product_id": p["id"], "qty": 1} for p in selected],
            "payment_method": "credit", "amount_paid": 0,
        })
        assert invoice.status_code == 201, invoice.text
        pdf = client.get(f"/invoices/{invoice.json()['id']}/pdf", headers=headers)
        assert pdf.status_code == 200, pdf.text
        pages = PdfReader(io.BytesIO(pdf.content)).pages
        assert len(pages) > 1
        page_text = [page.extract_text() or "" for page in pages]
        assert all("Product Name" in text for text in page_text[:-1]), [text[:500] for text in page_text]
        assert all(f"Page {number} of {len(pages)}" in "\n".join(page_text) for number in range(1, len(pages)+1))


def test_sale_and_invoice_void_are_recorded_in_stock_ledger():
    with TestClient(app) as client:
        headers = login_as_admin(client)
        product = client.post("/products", headers=headers, json={
            "name": "Ledger Seed", "brand": "Farm Co", "sku": "LEDGER-1",
            "category": "Fertilizers", "unit": "Bag", "selling_price": 120,
            "purchase_price_including_gst": 100, "gst_rate": 0, "stock": 5,
        })
        assert product.status_code == 201, product.text
        assert product.json()["brand"] == "Farm Co"
        invoice = client.post("/invoices", headers=headers, json={
            "items": [{"product_id": product.json()["id"], "qty": 2}],
            "payment_method": "cash", "amount_paid": 240,
        })
        assert invoice.status_code == 201, invoice.text
        movements = client.get(f"/products/{product.json()['id']}/stock-movements", headers=headers).json()
        assert movements[0]["movement_type"] == "SALE"
        assert float(movements[0]["previous_stock"]) == 5 and float(movements[0]["new_stock"]) == 3

        deleted = client.delete(f"/invoices/{invoice.json()['id']}", headers=headers)
        assert deleted.status_code == 204
        restored = client.get(f"/products/{product.json()['id']}", headers=headers).json()
        assert restored["stock"] == 5
        movements = client.get(f"/products/{product.json()['id']}/stock-movements", headers=headers).json()
        assert [item["movement_type"] for item in movements[:2]] == ["SALE_RETURN", "SALE"]


def test_purchase_receipt_updates_stock_price_and_purchase_report():
    with TestClient(app) as client:
        headers = login_as_admin(client)
        product = client.post("/products", headers=headers, json={
            "name": "Purchase Flow Fertilizer", "sku": "PURCHASE-FLOW-1",
            "category": "Fertilizers", "unit": "Bag", "selling_price": 150,
            "purchase_price_including_gst": 100, "gst_rate": 5, "stock": 2,
        }).json()
        received = client.post(f"/products/{product['id']}/stock-movements", headers=headers, json={
            "movement_type": "PURCHASE", "quantity": 3, "unit_purchase_price": 118,
            "supplier": "Green Wholesale", "reference_number": "GRN-2026-01",
            "purchase_date": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        })
        assert received.status_code == 201, received.text
        updated = client.get(f"/products/{product['id']}", headers=headers).json()
        assert updated["stock"] == 5
        internal = client.get("/products/internal", headers=headers).json()
        saved = next(item for item in internal if item["id"] == product["id"])
        assert float(saved["purchase_price_including_gst"]) == 100
        entries = client.get("/products/purchases", headers=headers)
        assert entries.status_code == 200 and len(entries.json()) == 1
        assert float(entries.json()[0]["total_amount"]) == 354
        assert float(entries.json()[0]["master_buying_price_snapshot"]) == 100
        assert float(entries.json()[0]["master_selling_price_snapshot"]) == 150
        assert entries.json()[0]["product_name"] == "Purchase Flow Fertilizer"
        purchase_rows = client.get("/accounting/purchase-rows", headers=headers)
        assert purchase_rows.status_code == 200
        assert purchase_rows.json()[0]["actual_purchase_rate"] == 118
        assert purchase_rows.json()[0]["buying_price"] == 100
        purchase_xlsx = client.get("/accounting/purchases/export?format=xlsx", headers=headers)
        assert purchase_xlsx.status_code == 200 and purchase_xlsx.content.startswith(b"PK")
        from openpyxl import load_workbook
        purchase_sheet = load_workbook(io.BytesIO(purchase_xlsx.content), data_only=True)["Purchases"]
        assert purchase_sheet.cell(3, 6).value == 118
        assert purchase_sheet.cell(3, 7).value == 100
        purchase_pdf = client.get("/accounting/purchases/export?format=pdf", headers=headers)
        assert purchase_pdf.status_code == 200 and purchase_pdf.content.startswith(b"%PDF-")
        report = client.get("/reports/data?range=today", headers=headers)
        assert report.status_code == 200 and report.json()["purchases"] == 354


def test_product_csv_import_is_previewable_idempotent_and_preserves_stock():
    with TestClient(app) as client:
        headers = login_as_admin(client)
        row = {
            "name": "Imported Product", "brand": "Seed House", "sku": "IMPORT-1",
            "category": "Fertilizers", "unit": "Bag", "pack_size": "25 Kg",
            "selling_price": 200, "purchase_price_including_gst": 150,
            "gst_rate": 5, "stock": 12, "initial_quantity": 12,
        }
        before_count = len(client.get("/products", headers=headers).json())
        preview = client.post("/products/import?dry_run=true", headers=headers, json={"products": [row]})
        assert preview.status_code == 200 and preview.json()["rows"][0]["status"] == "new"
        assert len(client.get("/products", headers=headers).json()) == before_count
        imported = client.post("/products/import?dry_run=false", headers=headers, json={"products": [row]})
        assert imported.status_code == 200 and imported.json()["created"] == 1
        product = client.get("/products?search=Seed%20House", headers=headers).json()[0]
        assert product["brand"] == "Seed House" and product["stock"] == 12

        changed = {**row, "selling_price": 215, "stock": 99}
        repeat = client.post("/products/import?dry_run=false", headers=headers, json={"products": [changed]})
        assert repeat.status_code == 200 and repeat.json()["matched"] == 1
        product = client.get(f"/products/{product['id']}", headers=headers).json()
        assert float(product["selling_price"]) == 215 and product["stock"] == 12
        exported = client.get("/products/export.csv", headers=headers)
        assert exported.status_code == 200 and "purchase_price_including_gst" in exported.text


def test_ambiguous_csv_duplicate_is_rejected_without_creating_a_product():
    with TestClient(app) as client:
        headers = login_as_admin(client)
        first = {"name": "Same Product", "brand": "Brand X", "sku": "SAME-1",
                 "category": "Other Agricultural Products", "unit": "Bottle", "stock": 1}
        assert client.post("/products", headers=headers, json=first).status_code == 201
        duplicate = {**first, "sku": "SAME-2"}
        response = client.post("/products/import?dry_run=false", headers=headers, json={"products": [duplicate]})
        assert response.status_code == 409
        assert sum(1 for product in client.get("/products", headers=headers).json() if product["name"] == "Same Product") == 1


def test_catalog_migration_keeps_all_110_source_rows_and_conservative_metadata():
    with TestClient(app) as client:
        headers = login_as_admin(client)
        catalog = [item for item in client.get("/products", headers=headers).json() if item["sku"].startswith("AAS-")]
        assert len(catalog) == 110
        by_sku = {item["sku"]: item for item in catalog}
        internal = [item for item in client.get("/products/internal", headers=headers).json() if item["sku"].startswith("AAS-")]
        internal_by_sku = {item["sku"]: item for item in internal}
        assert len(internal_by_sku) == 110
        for serial, name, hsn, gst, opening_qty, unit, buying, selling, _category in PRODUCTS:
            item = internal_by_sku[f"AAS-{serial:03d}"]
            assert item["catalog_serial"] == serial
            assert item["name"] == name
            assert item["hsn_code"] == hsn
            assert Decimal(str(item["gst_rate"])) == Decimal(gst)
            assert item["unit"] == unit
            assert Decimal(str(item["purchase_price_including_gst"])) == Decimal(buying)
            assert item["selling_price"] is None if selling is None else Decimal(str(item["selling_price"])) == Decimal(selling)
            assert Decimal(str(item["initial_quantity"])) == Decimal(opening_qty)
        assert by_sku["AAS-001"]["brand"] == "Tata"
        assert by_sku["AAS-002"]["brand"] is None
        assert by_sku["AAS-003"]["category"] == "Insecticides"
        assert by_sku["AAS-025"]["category"] == "Agricultural Equipment"
        assert by_sku["AAS-027"]["category"] == "Fungicides"
        assert by_sku["AAS-007"]["selling_price"] is None
        assert by_sku["AAS-008"]["selling_price"] is None
        assert by_sku["AAS-091"]["selling_price"] is None
        categories = {item["category"] for item in catalog}
        assert categories <= {
            "Fertilizers", "Pesticides", "Herbicides", "Fungicides", "Insecticides",
            "Plant Growth Regulators", "Micronutrients", "Agricultural Equipment",
            "Other Agricultural Products",
        }
