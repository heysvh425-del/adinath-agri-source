"""SQLite schema setup and forward-only, versioned migrations."""

import re
from decimal import Decimal, ROUND_HALF_UP

from .. import models  # Register ORM tables before migration metadata is inspected.
from .connection import Base, engine
from .catalog import PRODUCTS
from .catalog_metadata import DEFAULT_SOURCE_CATEGORY, SOURCE_BRANDS, SOURCE_CATEGORIES

CATALOG_MIGRATION = "001_actual_product_catalog"
INVENTORY_LEDGER_MIGRATION = "003_inventory_import_ledger"
EXPLICIT_SELLING_PRICE_MIGRATION = "004_explicit_selling_price"
PRODUCT_GST_INVOICE_SNAPSHOT_MIGRATION = "005_product_gst_invoice_snapshots"
GST_BILLING_MIGRATION = "010_adinath_gst_billing_and_catalog"
PRODUCT_MASTER_ENRICHMENT_MIGRATION = "011_product_master_brand_category_pack_size"
PRODUCT_BRAND_SOURCE_REVIEW_MIGRATION = "012_product_brand_source_review"
PRODUCT_PDF_MASTER_MIGRATION = "013_pdf_product_master_and_descriptions"


def migrate_schema() -> None:
    """Create missing tables and apply idempotent upgrades to existing DBs."""
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS app_migrations (version VARCHAR(120) PRIMARY KEY)"
        )
        product_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(products)")}
        if "expires_on" not in product_columns:
            connection.exec_driver_sql("ALTER TABLE products ADD COLUMN expires_on DATETIME")
        if "supplier" not in product_columns:
            connection.exec_driver_sql("ALTER TABLE products ADD COLUMN supplier VARCHAR(160)")
        product_upgrades = {
            "description": "TEXT",
            "catalog_serial": "INTEGER",
            "brand": "VARCHAR(120)",
            "pack_size": "VARCHAR(80)",
            "selling_price": "NUMERIC(12, 2)",
            "purchase_price_including_gst": "NUMERIC(12, 2)",
            "purchase_price_excluding_gst": "NUMERIC(12, 2)",
            "purchase_gst_component": "NUMERIC(12, 2)",
            "gst_rate": "NUMERIC(5, 2)",
            "gst_selection_type": "VARCHAR(12) NOT NULL DEFAULT 'preset'",
            "selling_price_tax_mode": "VARCHAR(20) NOT NULL DEFAULT 'exclusive'",
            "initial_quantity": "NUMERIC(12, 2)",
            "source_reference": "VARCHAR(200)",
            "hsn_code": "VARCHAR(20)",
            "discount_percentage": "NUMERIC(5, 2)",
        }
        for column, sql_type in product_upgrades.items():
            if column not in product_columns:
                connection.exec_driver_sql(f"ALTER TABLE products ADD COLUMN {column} {sql_type}")
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_products_catalog_serial ON products (catalog_serial)"
        )
        movement_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(stock_movements)")}
        if "unit_purchase_price" not in movement_columns:
            connection.exec_driver_sql("ALTER TABLE stock_movements ADD COLUMN unit_purchase_price NUMERIC(12, 2)")
        connection.exec_driver_sql("UPDATE products SET selling_price = rate WHERE selling_price IS NULL")
        item_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(invoice_items)")}
        item_upgrades = {
            "gst_rate": "NUMERIC(5, 2)",
            "gst_selection_type": "VARCHAR(12)",
            "selling_price_tax_mode": "VARCHAR(20)",
            "taxable_amount": "NUMERIC(12, 2)",
            "tax_amount": "NUMERIC(12, 2)",
            "total_amount": "NUMERIC(12, 2)",
            "hsn_code": "VARCHAR(20)",
            "discount_amount": "NUMERIC(12, 2) NOT NULL DEFAULT 0",
            "cgst_amount": "NUMERIC(12, 2) NOT NULL DEFAULT 0",
            "sgst_amount": "NUMERIC(12, 2) NOT NULL DEFAULT 0",
            "igst_amount": "NUMERIC(12, 2) NOT NULL DEFAULT 0",
            "purchase_rate_snapshot": "NUMERIC(12, 2)",
            "purchase_cost_ex_tax_snapshot": "NUMERIC(12, 2)",
            "purchase_cost_incl_tax_snapshot": "NUMERIC(12, 2)",
        }
        for column, sql_type in item_upgrades.items():
            if column not in item_columns:
                connection.exec_driver_sql(f"ALTER TABLE invoice_items ADD COLUMN {column} {sql_type}")

        farmer_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(farmers)")}
        for column, sql_type in {
            "billing_address": "VARCHAR(500)", "delivery_address": "VARCHAR(500)",
            "gstin": "VARCHAR(20)", "state_name": "VARCHAR(80)", "state_code": "VARCHAR(2)",
        }.items():
            if column not in farmer_columns:
                connection.exec_driver_sql(f"ALTER TABLE farmers ADD COLUMN {column} {sql_type}")

        invoice_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(invoices)")}
        invoice_upgrades = {
            "customer_name": "VARCHAR(160)", "customer_address": "VARCHAR(500)",
            "customer_gstin": "VARCHAR(20)", "customer_phone": "VARCHAR(30)",
            "customer_state_name": "VARCHAR(80)", "customer_state_code": "VARCHAR(2)",
            "place_of_supply_state_name": "VARCHAR(80)", "place_of_supply_state_code": "VARCHAR(2)",
            "cgst_amount": "NUMERIC(12, 2) DEFAULT 0", "sgst_amount": "NUMERIC(12, 2) DEFAULT 0",
            "igst_amount": "NUMERIC(12, 2) DEFAULT 0", "rounding_adjustment": "NUMERIC(12, 2) DEFAULT 0",
            "amount_in_words": "VARCHAR(300)", "remarks": "VARCHAR(1000)",
            "payment_terms": "VARCHAR(300)", "due_date": "DATETIME", "advance_amount": "NUMERIC(12, 2) DEFAULT 0",
            "invoice_type": "VARCHAR(40) DEFAULT 'TAX INVOICE'", "irn": "VARCHAR(100)",
            "invoice_level_discount": "NUMERIC(12, 2) DEFAULT 0",
            "acknowledgement_number": "VARCHAR(100)", "acknowledgement_date": "DATETIME",
            "eway_bill_number": "VARCHAR(100)",
            "original_invoice_no": "VARCHAR(120)", "source_database": "VARCHAR(30)",
        }
        for column, sql_type in invoice_upgrades.items():
            if column not in invoice_columns:
                connection.exec_driver_sql(f"ALTER TABLE invoices ADD COLUMN {column} {sql_type}")

        purchase_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(purchase_entries)")}
        for column, sql_type in {
            "product_name": "VARCHAR",
            "hsn_code": "VARCHAR(20)",
            "master_buying_price_snapshot": "NUMERIC(12, 2)",
            "master_selling_price_snapshot": "NUMERIC(12, 2)",
        }.items():
            if column not in purchase_columns:
                connection.exec_driver_sql(f"ALTER TABLE purchase_entries ADD COLUMN {column} {sql_type}")

        # Rebuild only legacy databases whose rate column disallows NULL, since
        # two supplied products have no handwritten selling price.
        rate_column = next(
            (row for row in connection.exec_driver_sql("PRAGMA table_info(products)") if row[1] == "rate"),
            None,
        )
        if rate_column and rate_column[3]:
            # SQLite foreign keys are disabled by default for this application.
            # Preserve product IDs so historical invoice_items keep their links.
            connection.exec_driver_sql("ALTER TABLE products RENAME TO products_legacy_rate")
            connection.exec_driver_sql("""CREATE TABLE products (
                id INTEGER NOT NULL PRIMARY KEY, name VARCHAR NOT NULL, sku VARCHAR NOT NULL,
                brand VARCHAR(120), category VARCHAR NOT NULL, unit VARCHAR NOT NULL, rate NUMERIC(12, 2),
                stock NUMERIC(12, 2), min_stock NUMERIC(12, 2), expires_on DATETIME,
                supplier VARCHAR(160), is_active BOOLEAN, pack_size VARCHAR(80),
                description TEXT, catalog_serial INTEGER,
                selling_price NUMERIC(12, 2), purchase_price_including_gst NUMERIC(12, 2),
                purchase_price_excluding_gst NUMERIC(12, 2), purchase_gst_component NUMERIC(12, 2),
                gst_rate NUMERIC(5, 2), gst_selection_type VARCHAR(12) NOT NULL DEFAULT 'preset',
                selling_price_tax_mode VARCHAR(20) NOT NULL DEFAULT 'exclusive',
                initial_quantity NUMERIC(12, 2), source_reference VARCHAR(200),
                hsn_code VARCHAR(20), discount_percentage NUMERIC(5, 2))""")
            connection.exec_driver_sql("""INSERT INTO products
                (id,name,brand,sku,category,unit,rate,stock,min_stock,expires_on,supplier,is_active,pack_size,description,catalog_serial,
                 selling_price,purchase_price_including_gst,purchase_price_excluding_gst,purchase_gst_component,
                 gst_rate,gst_selection_type,selling_price_tax_mode,initial_quantity,source_reference,hsn_code,discount_percentage)
                SELECT id,name,brand,sku,category,unit,rate,stock,min_stock,expires_on,supplier,is_active,pack_size,description,catalog_serial,
                 selling_price,purchase_price_including_gst,purchase_price_excluding_gst,purchase_gst_component,
                 gst_rate,gst_selection_type,selling_price_tax_mode,initial_quantity,source_reference,hsn_code,discount_percentage
                FROM products_legacy_rate""")
            connection.exec_driver_sql("DROP TABLE products_legacy_rate")
            connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ix_products_sku ON products (sku)")
            connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_products_id ON products (id)")
            connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_products_catalog_serial ON products (catalog_serial)")

        # Older SQLite versions may rewrite invoice_items.product_id to the
        # temporary products_legacy_rate name during the nullable-rate table
        # rebuild above. Repair databases that already carry that stale FK.
        item_fks = list(connection.exec_driver_sql("PRAGMA foreign_key_list(invoice_items)"))
        if any(row[2] == "products_legacy_rate" for row in item_fks):
            connection.exec_driver_sql("ALTER TABLE invoice_items RENAME TO invoice_items_legacy_product_fk")
            connection.exec_driver_sql("""CREATE TABLE invoice_items (
                id INTEGER NOT NULL PRIMARY KEY, invoice_id INTEGER NOT NULL,
                product_id INTEGER, product_name VARCHAR NOT NULL, unit VARCHAR NOT NULL,
                qty NUMERIC(12, 2) NOT NULL, rate NUMERIC(12, 2) NOT NULL,
                amount NUMERIC(12, 2) NOT NULL, gst_rate NUMERIC(5, 2),
                gst_selection_type VARCHAR(12), selling_price_tax_mode VARCHAR(20),
                taxable_amount NUMERIC(12, 2), tax_amount NUMERIC(12, 2), total_amount NUMERIC(12, 2),
                hsn_code VARCHAR(20), discount_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
                cgst_amount NUMERIC(12, 2) NOT NULL DEFAULT 0, sgst_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
                igst_amount NUMERIC(12, 2) NOT NULL DEFAULT 0, purchase_rate_snapshot NUMERIC(12, 2),
                purchase_cost_ex_tax_snapshot NUMERIC(12, 2), purchase_cost_incl_tax_snapshot NUMERIC(12, 2),
                FOREIGN KEY(invoice_id) REFERENCES invoices(id),
                FOREIGN KEY(product_id) REFERENCES products(id))""")
            item_copy_columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(invoice_items_legacy_product_fk)")]
            item_copy_names = ", ".join(item_copy_columns)
            connection.exec_driver_sql(
                f"INSERT INTO invoice_items ({item_copy_names}) SELECT {item_copy_names} FROM invoice_items_legacy_product_fk"
            )
            connection.exec_driver_sql("DROP TABLE invoice_items_legacy_product_fk")
            connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_invoice_items_id ON invoice_items (id)")

        invoice_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(invoices)")}
        if "client_request_id" not in invoice_columns:
            connection.exec_driver_sql("ALTER TABLE invoices ADD COLUMN client_request_id VARCHAR(64)")
        if "client_payload_hash" not in invoice_columns:
            connection.exec_driver_sql("ALTER TABLE invoices ADD COLUMN client_payload_hash VARCHAR(64)")
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_invoices_client_request_id "
            "ON invoices (client_request_id)"
        )
        connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_invoices_date ON invoices (date)")
        connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_invoices_farmer_id ON invoices (farmer_id)")
        connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_invoices_balance_due ON invoices (balance_due)")
        # The project uses forward-only SQLAlchemy/SQLite migrations rather
        # than Alembic. Record this additive inventory migration after all
        # declared tables and columns have been created successfully.
        connection.exec_driver_sql(
            "INSERT OR IGNORE INTO app_migrations(version) VALUES (?)",
            (INVENTORY_LEDGER_MIGRATION,),
        )
        connection.exec_driver_sql(
            "INSERT OR IGNORE INTO app_migrations(version) VALUES (?)",
            (EXPLICIT_SELLING_PRICE_MIGRATION,),
        )
        connection.exec_driver_sql(
            "INSERT OR IGNORE INTO app_migrations(version) VALUES (?)",
            (PRODUCT_GST_INVOICE_SNAPSHOT_MIGRATION,),
        )
        connection.exec_driver_sql(
            "INSERT OR IGNORE INTO business_settings "
            "(id,business_name,address,gstin,state_name,state_code,phone,declaration) "
            "VALUES (1,'ADINATH AGRI SERVICES','SH-72, Ugar Budruk, Kusnal Road, Karnataka - 591316','29ACPFA8181K1ZM','Karnataka','29','8431271008',?)",
            ("We declare that this invoice shows the actual price of the goods described and that all particulars are true and correct.",),
        )

        # Reconcile only the 110 rows that this project previously imported
        # under AAS-001..AAS-110. Never touch unrelated/user-created products.
        marker = "Invoice pages 1-4 / source manifest 110 products"
        for serial, name, hsn, gst, opening_qty, unit, buying, selling, category in PRODUCTS:
            if connection.exec_driver_sql(
                "SELECT 1 FROM app_migrations WHERE version = ?", (GST_BILLING_MIGRATION,)
            ).first():
                break
            sku = f"AAS-{serial:03d}"
            found = connection.exec_driver_sql(
                "SELECT id, source_reference, stock, initial_quantity, purchase_price_including_gst, selling_price "
                "FROM products WHERE sku = ?", (sku,)
            ).first()
            quantity = float(opening_qty)
            buy = float(buying)
            sale = None if selling is None else float(selling)
            excluding = round(buy / (1 + float(gst) / 100), 2)
            component = round(buy - excluding, 2)
            if not found:
                connection.exec_driver_sql(
                    "INSERT INTO products (name, sku, category, unit, rate, selling_price, stock, min_stock, "
                    "is_active, purchase_price_including_gst, purchase_price_excluding_gst, purchase_gst_component, "
                    "gst_rate, gst_selection_type, selling_price_tax_mode, initial_quantity, source_reference, hsn_code) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 10, 1, ?, ?, ?, ?, 'preset', 'exclusive', ?, ?, ?)",
                    (name, sku, category, unit, sale, sale, quantity, buy, excluding, component,
                     float(gst), quantity, marker, hsn),
                )
                continue
            product_id, source_ref, stock, initial_qty, old_buy, old_sale = found
            if not source_ref or not (source_ref == marker or "source manifest 110 products" in source_ref):
                continue
            if old_buy != buy or old_sale != sale:
                connection.exec_driver_sql(
                    "INSERT INTO product_price_history (product_id, old_purchase_price, new_purchase_price, "
                    "old_selling_price, new_selling_price, changed_at, reason) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)",
                    (product_id, old_buy, buy, old_sale, sale, "Reconciled from Adinath Agri Services buying price PDF"),
                )
            connection.exec_driver_sql(
                "UPDATE products SET name=?, category=?, unit=?, rate=?, selling_price=?, "
                "purchase_price_including_gst=?, purchase_price_excluding_gst=?, purchase_gst_component=?, "
                "gst_rate=?, gst_selection_type='preset', hsn_code=?, initial_quantity=COALESCE(initial_quantity, ?), "
                "source_reference=? WHERE id=?",
                (name, category, unit, sale, sale, buy, excluding, component, float(gst), hsn,
                 quantity, marker, product_id),
            )
        connection.exec_driver_sql(
            "INSERT OR IGNORE INTO app_migrations(version) VALUES (?)", (GST_BILLING_MIGRATION,)
        )

        enrichment_done = connection.exec_driver_sql(
            "SELECT 1 FROM app_migrations WHERE version = ?", (PRODUCT_MASTER_ENRICHMENT_MIGRATION,)
        ).first()
        if not enrichment_done:
            size_pattern = re.compile(r"\b\d+(?:\.\d+)?\s*(?:Gms?|Ml|Ltr|Kg)\b", re.IGNORECASE)
            for serial, name, *_rest in PRODUCTS:
                sku = f"AAS-{serial:03d}"
                match = size_pattern.search(name)
                pack_size = match.group(0) if match else None
                if serial == 26:
                    pack_size = "20 Ltr Pump"
                connection.exec_driver_sql(
                    "UPDATE products SET brand=?, category=?, pack_size=COALESCE(pack_size, ?) "
                    "WHERE sku=? AND (source_reference LIKE '%source manifest 110 products%' "
                    "OR source_reference LIKE '%source manifest 110 products%' OR sku=?)",
                    (SOURCE_BRANDS.get(serial), SOURCE_CATEGORIES.get(serial, DEFAULT_SOURCE_CATEGORY),
                     pack_size, sku, sku),
                )
            connection.exec_driver_sql(
                "INSERT OR IGNORE INTO app_migrations(version) VALUES (?)",
                (PRODUCT_MASTER_ENRICHMENT_MIGRATION,),
            )
        if not connection.exec_driver_sql(
            "SELECT 1 FROM app_migrations WHERE version = ?", (PRODUCT_BRAND_SOURCE_REVIEW_MIGRATION,)
        ).first():
            # The PDF lists Burn Out without a brand attribution; remove a
            # manufacturer-derived value so the master reflects only sourced data.
            connection.exec_driver_sql(
                "UPDATE products SET brand=NULL WHERE sku='AAS-002' "
                "AND source_reference LIKE '%source manifest 110 products%'"
            )
            connection.exec_driver_sql(
                "INSERT OR IGNORE INTO app_migrations(version) VALUES (?)",
                (PRODUCT_BRAND_SOURCE_REVIEW_MIGRATION,),
            )

        # Reconcile the 110 official fields from the supplied PDF by serial
        # SKU. Never reset live stock or replace user-entered descriptions.
        if not connection.exec_driver_sql(
            "SELECT 1 FROM app_migrations WHERE version = ?", (PRODUCT_PDF_MASTER_MIGRATION,)
        ).first():
            marker = "Adinath_Agri_Services_Buying_Price.pdf; serials 1-110"
            for serial, name, hsn, gst, opening_qty, unit, buying, selling, category in PRODUCTS:
                sku = f"AAS-{serial:03d}"
                opening = Decimal(opening_qty)
                buy = Decimal(buying)
                sale = None if selling is None else Decimal(selling)
                gst_rate = Decimal(gst)
                ex_tax = (buy / (Decimal("1") + gst_rate / Decimal("100"))).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                gst_component = buy - ex_tax
                found = connection.exec_driver_sql(
                    "SELECT id, purchase_price_including_gst, selling_price FROM products WHERE sku = ?",
                    (sku,),
                ).first()
                if not found:
                    connection.exec_driver_sql(
                        """INSERT INTO products
                        (name, catalog_serial, sku, category, unit, rate, selling_price, stock, min_stock,
                         is_active, purchase_price_including_gst, purchase_price_excluding_gst,
                         purchase_gst_component, gst_rate, gst_selection_type, selling_price_tax_mode,
                         initial_quantity, source_reference, hsn_code)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 10, 1, ?, ?, ?, ?, 'preset', 'exclusive', ?, ?, ?)""",
                        (name, serial, sku, category, unit,
                         None if sale is None else str(sale), None if sale is None else str(sale),
                         str(opening), str(buy), str(ex_tax), str(gst_component), str(gst_rate),
                         str(opening), marker, hsn),
                    )
                    product_id = connection.exec_driver_sql(
                        "SELECT id FROM products WHERE sku = ?", (sku,)
                    ).scalar_one()
                    connection.exec_driver_sql(
                        """INSERT INTO stock_movements
                        (product_id, movement_type, quantity_in, quantity_out, previous_stock, new_stock,
                         reference_number, movement_date, notes, created_by)
                        VALUES (?, 'OPENING_STOCK', ?, 0, 0, ?, ?, CURRENT_TIMESTAMP, ?, NULL)""",
                        (product_id, str(opening), str(opening), marker, "Opening quantity from official PDF"),
                    )
                    continue

                product_id, old_buy, old_sale = found
                if old_buy != buy or old_sale != sale:
                    connection.exec_driver_sql(
                        """INSERT INTO product_price_history
                        (product_id, old_purchase_price, new_purchase_price, old_selling_price,
                         new_selling_price, changed_at, reason)
                        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)""",
                        (product_id, old_buy, str(buy), old_sale,
                         None if sale is None else str(sale), "Official PDF product master reconciliation"),
                    )
                connection.exec_driver_sql(
                    """UPDATE products SET name=?, catalog_serial=?, unit=?, rate=?, selling_price=?,
                       purchase_price_including_gst=?, purchase_price_excluding_gst=?, purchase_gst_component=?,
                       gst_rate=?, gst_selection_type='preset', initial_quantity=?, source_reference=?, hsn_code=?
                       WHERE id=?""",
                    (name, serial, unit, None if sale is None else str(sale),
                     None if sale is None else str(sale), str(buy), str(ex_tax), str(gst_component),
                     str(gst_rate), str(opening), marker, hsn, product_id),
                )
                has_movement = connection.exec_driver_sql(
                    "SELECT 1 FROM stock_movements WHERE product_id=? LIMIT 1", (product_id,)
                ).first()
                current_stock = connection.exec_driver_sql(
                    "SELECT stock FROM products WHERE id=?", (product_id,)
                ).scalar_one()
                if not has_movement and Decimal(str(current_stock or 0)) == opening:
                    connection.exec_driver_sql(
                        """INSERT INTO stock_movements
                        (product_id, movement_type, quantity_in, quantity_out, previous_stock, new_stock,
                         reference_number, movement_date, notes, created_by)
                        VALUES (?, 'OPENING_STOCK', ?, 0, 0, ?, ?, CURRENT_TIMESTAMP, ?, NULL)""",
                        (product_id, str(opening), str(opening), marker, "Opening quantity from official PDF"),
                    )
            connection.exec_driver_sql(
                "INSERT OR IGNORE INTO app_migrations(version) VALUES (?)", (PRODUCT_PDF_MASTER_MIGRATION,)
            )


def seed_products() -> None:
    """Retire the legacy hardcoded catalog seed without touching product data.

    Product records are now managed explicitly through database-backed product
    workflows and versioned imports. Older versions of this function rewrote
    names/prices and could delete or deactivate records at first startup.
    """
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS app_migrations (version VARCHAR(120) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT OR IGNORE INTO app_migrations(version) VALUES (?)",
            ("002_retire_mutating_catalog_seed",),
        )


def initialize_database() -> None:
    """Run schema migrations followed by the repeatable catalog seed."""
    migrate_schema()
    seed_products()
