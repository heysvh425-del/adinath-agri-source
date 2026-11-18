"""Back up, migrate, and merge localhost business data into the desktop DB.

This is intentionally a one-time, forward-only data operation. It preserves
both invoice histories and tags each invoice with its source database. Local
invoice numbers that collide with desktop invoice numbers receive an ``LCL-``
prefix; the original number is retained in ``original_invoice_no``.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4


MERGE_MARKER = "014_merge_localhost_history_into_desktop"
BACKEND_DIR = Path(__file__).resolve().parent
SOURCE_DEFAULT = BACKEND_DIR / "adinath.db"
APP_DATA = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
TARGET_DEFAULT = APP_DATA / "adinath-agri-services-desktop" / "data" / "adinath.db"


def backup_database(source: Path, backup: Path) -> None:
    backup.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(backup) as dst:
        src.backup(dst)


def migrate_database(path: Path) -> None:
    env = os.environ.copy()
    env["ADINATH_DB_PATH"] = str(path)
    subprocess.run(
        [sys.executable, "-c", "from app.database.migrations import initialize_database; initialize_database()"],
        cwd=BACKEND_DIR,
        env=env,
        check=True,
    )


def table_columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]


def insert_copy(db: sqlite3.Connection, table: str, row: sqlite3.Row,
                overrides: dict | None = None) -> int:
    columns = [column for column in table_columns(db, table) if column != "id" and column in row.keys()]
    values = {column: row[column] for column in columns}
    values.update(overrides or {})
    names = list(values)
    quoted_names = ", ".join('"' + name + '"' for name in names)
    sql = (f'INSERT INTO "{table}" ({quoted_names}) '
           f'VALUES ({", ".join("?" for _ in names)})')
    cursor = db.execute(sql, [values[name] for name in names])
    return int(cursor.lastrowid)


def amount(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def farmer_key(row: sqlite3.Row) -> tuple[str, str, str]:
    digits = "".join(char for char in (row["mobile"] or "") if char.isdigit())
    return ((row["name"] or "").strip().casefold(), digits, (row["gstin"] or "").strip().casefold())


def sales_by_product(db: sqlite3.Connection) -> dict[int, Decimal]:
    return {
        row[0]: Decimal(str(row[1] or 0))
        for row in db.execute("SELECT product_id, SUM(qty) FROM invoice_items WHERE product_id IS NOT NULL GROUP BY product_id")
    }


def verify_catalog_and_stock(source: sqlite3.Connection, target: sqlite3.Connection) -> tuple[dict, dict]:
    source_products = {row["sku"]: row for row in source.execute("SELECT * FROM products")}
    target_products = {row["sku"]: row for row in target.execute("SELECT * FROM products")}
    source_catalog = {sku: row for sku, row in source_products.items() if sku.startswith("AAS-")}
    target_catalog = {sku: row for sku, row in target_products.items() if sku.startswith("AAS-")}
    if len(source_catalog) != 110 or len(target_catalog) != 110:
        raise RuntimeError(f"Expected 110 PDF catalog SKUs in each database; found {len(source_catalog)} and {len(target_catalog)}")

    source_sales = sales_by_product(source)
    target_sales = sales_by_product(target)
    compare_fields = ("name", "catalog_serial", "hsn_code", "gst_rate", "unit", "initial_quantity",
                      "purchase_price_including_gst", "selling_price")
    for sku in sorted(source_catalog):
        left, right = source_catalog[sku], target_catalog.get(sku)
        if right is None:
            raise RuntimeError(f"Desktop database is missing catalog product {sku}")
        for field in compare_fields:
            if str(left[field]) != str(right[field]):
                raise RuntimeError(f"Catalog mismatch for {sku}.{field}: {left[field]!r} vs {right[field]!r}")
        opening = amount(left["initial_quantity"])
        for label, product, sold in (("localhost", left, source_sales), ("desktop", right, target_sales)):
            expected = opening - Decimal(str(sold.get(product["id"], 0)))
            actual = amount(product["stock"])
            if actual != expected:
                raise RuntimeError(f"{label} stock check failed for {sku}: database={actual}, opening minus sales={expected}")

    for label, db in (("localhost", source), ("desktop", target)):
        non_opening = db.execute(
            "SELECT movement_type, COUNT(*) FROM stock_movements WHERE movement_type <> 'OPENING_STOCK' GROUP BY movement_type"
        ).fetchall()
        if non_opening:
            raise RuntimeError(f"{label} has non-opening stock movements; review stock reconciliation before merging: {non_opening}")
        if db.execute("SELECT COUNT(*) FROM purchase_entries").fetchone()[0]:
            raise RuntimeError(f"{label} has purchase entries; review stock reconciliation before merging")
    return source_products, target_products


def merge(source_path: Path, target_path: Path) -> dict:
    if source_path.resolve() == target_path.resolve():
        raise RuntimeError("Source and target database paths are the same")
    if not source_path.is_file() or not target_path.is_file():
        raise FileNotFoundError("Both localhost and desktop database files must already exist")

    with sqlite3.connect(target_path) as check:
        check.row_factory = sqlite3.Row
        has_marker = check.execute(
            "SELECT 1 FROM app_migrations WHERE version=?", (MERGE_MARKER,)
        ).fetchone()
        if has_marker:
            print(f"Merge already completed ({MERGE_MARKER}); no database changes made.")
            return {"already_merged": True}

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    source_backup = source_path.parent / "backups" / f"adinath-localhost-before-merge-{stamp}.db"
    target_backup = target_path.parent / "backups" / f"adinath-desktop-before-merge-{stamp}.db"
    backup_database(source_path, source_backup)
    backup_database(target_path, target_backup)
    print(f"Backed up localhost: {source_backup}")
    print(f"Backed up desktop:   {target_backup}")

    migrate_database(source_path)
    migrate_database(target_path)

    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(target_path)
    source.row_factory = target.row_factory = sqlite3.Row
    source.execute("PRAGMA foreign_keys=ON")
    target.execute("PRAGMA foreign_keys=ON")
    try:
        source_settings = [tuple(row) for row in source.execute("SELECT * FROM business_settings ORDER BY id")]
        target_settings = [tuple(row) for row in target.execute("SELECT * FROM business_settings ORDER BY id")]
        if source_settings != target_settings:
            raise RuntimeError("Business settings differ between the databases; reconcile them before merging")

        source_products, target_products = verify_catalog_and_stock(source, target)
        source_products_by_id = {row["id"]: row for row in source_products.values()}
        source_invoice_rows = source.execute("SELECT * FROM invoices ORDER BY id").fetchall()
        source_item_count = source.execute("SELECT COUNT(*) FROM invoice_items").fetchone()[0]
        source_payment_count = source.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        source_invoice_total = source.execute("SELECT COALESCE(SUM(grand_total), 0) FROM invoices").fetchone()[0]
        source_item_total = source.execute("SELECT COALESCE(SUM(amount), 0) FROM invoice_items").fetchone()[0]
        source_payment_total = source.execute("SELECT COALESCE(SUM(amount), 0) FROM payments").fetchone()[0]

        existing_numbers = {row[0] for row in target.execute("SELECT invoice_no FROM invoices")}
        collisions = sum(1 for row in source_invoice_rows if row["invoice_no"] in existing_numbers)
        if collisions != 18:
            raise RuntimeError(f"Expected 18 invoice-number conflicts for the confirmed merge; found {collisions}")

        target.execute("BEGIN IMMEDIATE")
        target.execute(
            "UPDATE invoices SET source_database=COALESCE(source_database, 'desktop'), "
            "original_invoice_no=COALESCE(original_invoice_no, invoice_no)"
        )

        user_map = {}
        canonical_users = {row["username"].casefold(): row["id"] for row in target.execute("SELECT id,username FROM users")}
        for user in source.execute("SELECT * FROM users ORDER BY id"):
            match = canonical_users.get(user["username"].casefold())
            if match is None:
                match = insert_copy(target, "users", user)
                canonical_users[user["username"].casefold()] = match
            user_map[user["id"]] = match

        farmer_map = {}
        canonical_farmers = {}
        for farmer in target.execute("SELECT * FROM farmers ORDER BY id"):
            canonical_farmers.setdefault(farmer_key(farmer), farmer["id"])
        for farmer in source.execute("SELECT * FROM farmers ORDER BY id"):
            key = farmer_key(farmer)
            match = canonical_farmers.get(key)
            if match is None:
                match = insert_copy(target, "farmers", farmer)
                canonical_farmers[key] = match
            farmer_map[farmer["id"]] = match

        target_products = {row["sku"]: row for row in target.execute("SELECT * FROM products")}
        source_to_target_product = {}
        stock_updates = {}
        for sku, product in source_products.items():
            match = target_products.get(sku)
            if match is None:
                new_id = insert_copy(target, "products", product)
                source_to_target_product[product["id"]] = new_id
                continue
            if not sku.startswith("AAS-"):
                raise RuntimeError(f"Unexpected product SKU overlap outside the official catalog: {sku}")
            source_to_target_product[product["id"]] = match["id"]
            opening = amount(product["initial_quantity"])
            merged_stock = amount(match["stock"]) + amount(product["stock"]) - opening
            if merged_stock < 0:
                raise RuntimeError(f"Combined sales would make {sku} stock negative ({merged_stock})")
            stock_updates[match["id"]] = merged_stock
        for product_id, stock in stock_updates.items():
            target.execute("UPDATE products SET stock=? WHERE id=?", (str(stock), product_id))

        invoice_map = {}
        remapped_numbers = []
        for invoice in source_invoice_rows:
            original_number = invoice["original_invoice_no"] or invoice["invoice_no"]
            invoice_number = invoice["invoice_no"]
            if invoice_number in existing_numbers:
                base = f"LCL-{invoice_number}"
                invoice_number = base
                suffix = 2
                while invoice_number in existing_numbers:
                    invoice_number = f"{base}-{suffix}"
                    suffix += 1
                remapped_numbers.append((original_number, invoice_number))
            existing_numbers.add(invoice_number)
            overrides = {
                "invoice_no": invoice_number,
                "original_invoice_no": original_number,
                "source_database": "localhost",
                "farmer_id": farmer_map.get(invoice["farmer_id"]) if invoice["farmer_id"] is not None else None,
                "user_id": user_map.get(invoice["user_id"]) if invoice["user_id"] is not None else None,
            }
            request_id = invoice["client_request_id"]
            if request_id and target.execute("SELECT 1 FROM invoices WHERE client_request_id=?", (request_id,)).fetchone():
                overrides["client_request_id"] = f"lcl-{uuid4().hex}"
            new_id = insert_copy(target, "invoices", invoice, overrides)
            invoice_map[invoice["id"]] = new_id

        for item in source.execute("SELECT * FROM invoice_items ORDER BY id"):
            insert_copy(target, "invoice_items", item, {
                "invoice_id": invoice_map[item["invoice_id"]],
                "product_id": source_to_target_product.get(item["product_id"]) if item["product_id"] is not None else None,
            })
        for payment in source.execute("SELECT * FROM payments ORDER BY id"):
            insert_copy(target, "payments", payment, {
                "invoice_id": invoice_map[payment["invoice_id"]],
                "user_id": user_map.get(payment["user_id"]) if payment["user_id"] is not None else None,
            })
        for movement in source.execute("SELECT * FROM stock_movements ORDER BY id"):
            product = source_products_by_id[movement["product_id"]]
            if product["sku"].startswith("AAS-") and movement["movement_type"] == "OPENING_STOCK":
                continue
            insert_copy(target, "stock_movements", movement, {
                "product_id": source_to_target_product[movement["product_id"]],
                "created_by": user_map.get(movement["created_by"]) if movement["created_by"] is not None else None,
            })
        for entry in source.execute("SELECT * FROM purchase_entries ORDER BY id"):
            insert_copy(target, "purchase_entries", entry, {
                "product_id": source_to_target_product[entry["product_id"]],
                "created_by": user_map.get(entry["created_by"]) if entry["created_by"] is not None else None,
            })
        for history in source.execute("SELECT * FROM product_price_history ORDER BY id"):
            insert_copy(target, "product_price_history", history, {
                "product_id": source_to_target_product[history["product_id"]],
                "changed_by": user_map.get(history["changed_by"]) if history["changed_by"] is not None else None,
            })
        for batch in source.execute("SELECT * FROM import_batches ORDER BY id"):
            reference = batch["source_reference"]
            if target.execute("SELECT 1 FROM import_batches WHERE source_reference=?", (reference,)).fetchone():
                reference = f"localhost | {reference}"[:200]
                suffix = 2
                while target.execute("SELECT 1 FROM import_batches WHERE source_reference=?", (reference,)).fetchone():
                    reference = f"localhost | {batch['source_reference']} ({suffix})"[:200]
                    suffix += 1
            insert_copy(target, "import_batches", batch, {"source_reference": reference})

        for migration in source.execute("SELECT version FROM app_migrations"):
            target.execute("INSERT OR IGNORE INTO app_migrations(version) VALUES (?)", (migration["version"],))
        target.execute("INSERT INTO app_migrations(version) VALUES (?)", (MERGE_MARKER,))

        # Validate preserved source transaction counts and totals before commit.
        merged_invoice_count = target.execute("SELECT COUNT(*) FROM invoices WHERE source_database='localhost'").fetchone()[0]
        merged_item_count = target.execute("SELECT COUNT(*) FROM invoice_items i JOIN invoices v ON v.id=i.invoice_id WHERE v.source_database='localhost'").fetchone()[0]
        merged_payment_count = target.execute("SELECT COUNT(*) FROM payments p JOIN invoices v ON v.id=p.invoice_id WHERE v.source_database='localhost'").fetchone()[0]
        invoice_total = target.execute("SELECT COALESCE(SUM(grand_total), 0) FROM invoices WHERE source_database='localhost'").fetchone()[0]
        item_total = target.execute("SELECT COALESCE(SUM(i.amount), 0) FROM invoice_items i JOIN invoices v ON v.id=i.invoice_id WHERE v.source_database='localhost'").fetchone()[0]
        payment_total = target.execute("SELECT COALESCE(SUM(p.amount), 0) FROM payments p JOIN invoices v ON v.id=p.invoice_id WHERE v.source_database='localhost'").fetchone()[0]
        if (merged_invoice_count, merged_item_count, merged_payment_count) != (
            len(source_invoice_rows), source_item_count, source_payment_count
        ):
            raise RuntimeError("Merged invoice, item, or payment counts do not match localhost source counts")
        if (amount(invoice_total), amount(item_total), amount(payment_total)) != (
            amount(source_invoice_total), amount(source_item_total), amount(source_payment_total)
        ):
            raise RuntimeError("Merged localhost invoice, item, or payment totals do not match the source")
        target.commit()
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()

    with sqlite3.connect(target_path) as db:
        product_count = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        invoice_count = db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        item_count = db.execute("SELECT COUNT(*) FROM invoice_items").fetchone()[0]
        payment_count = db.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        movement_count = db.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0]
        history_count = db.execute("SELECT COUNT(*) FROM product_price_history").fetchone()[0]
    result = {
        "localhost_invoices_merged": len(source_invoice_rows),
        "invoice_number_conflicts_remapped": len(remapped_numbers),
        "remapped_invoice_numbers": remapped_numbers,
        "products": product_count,
        "invoices": invoice_count,
        "invoice_items": item_count,
        "payments": payment_count,
        "stock_movements": movement_count,
        "price_history_rows": history_count,
        "localhost_backup": str(source_backup),
        "desktop_backup": str(target_backup),
    }
    print("Shared database merge completed:")
    for key, value in result.items():
        print(f"  {key}: {value}")
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE_DEFAULT)
    parser.add_argument("--target", type=Path, default=TARGET_DEFAULT)
    args = parser.parse_args()
    merge(args.source, args.target)
