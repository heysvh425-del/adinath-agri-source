"""Compare the source buying-price PDF, catalog manifest and desktop DB."""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.database.catalog import PRODUCTS  # noqa: E402
from app.database.catalog_metadata import DEFAULT_SOURCE_CATEGORY, SOURCE_BRANDS, SOURCE_CATEGORIES  # noqa: E402

DEFAULT_PDF = Path(r"C:\Users\Sanket Hsoure\Downloads\Adinath_Agri_Services_Buying_Price (1).pdf")
DEFAULT_DB = Path(r"C:\Users\Sanket Hsoure\AppData\Roaming\adinath-agri-services-desktop\data\adinath.db")
VALID_CATEGORIES = {
    "Fertilizers", "Pesticides", "Herbicides", "Fungicides", "Insecticides",
    "Plant Growth Regulators", "Micronutrients", "Agricultural Equipment",
    "Other Agricultural Products",
}
QTY_LINE = re.compile(r"^(\d+(?:\.\d+)?)\s+(NOS|BOTTOL|Packets|Bags)$", re.IGNORECASE)
NUMBER = re.compile(r"^[\d,]+(?:\.\d+)?$")


def parse_pdf(path: Path) -> list[dict[str, object]]:
    lines = [line.strip() for page in PdfReader(str(path)).pages
             for line in (page.extract_text() or "").splitlines() if line.strip()]
    result = []
    for index, value in enumerate(lines):
        if not value.isdigit():
            continue
        serial = int(value)
        if serial != len(result) + 1 or index + 7 >= len(lines):
            continue
        name, hsn, gst, qty_line, buying, _per_unit, selling = lines[index + 1:index + 8]
        qty_match = QTY_LINE.fullmatch(qty_line)
        if not (qty_match and re.fullmatch(r"\d{6,8}", hsn) and re.fullmatch(r"\d+(?:\.\d+)?%", gst)
                and NUMBER.fullmatch(buying) and (NUMBER.fullmatch(selling) or selling == "\u2014")):
            continue
        result.append({
            "serial": serial, "name": name, "hsn": hsn, "gst": Decimal(gst.rstrip("%")),
            "quantity": Decimal(qty_match.group(1)), "unit": qty_match.group(2),
            "buying": Decimal(buying.replace(",", "")),
            "selling": None if selling == "\u2014" else Decimal(selling.replace(",", "")),
        })
    return result


def decimal_or_none(value):
    return None if value is None else Decimal(str(value))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    args = parser.parse_args()
    if not args.pdf.is_file():
        raise SystemExit(f"Source PDF not found: {args.pdf}")
    if not args.db.is_file():
        raise SystemExit(f"Desktop database not found: {args.db}")

    source = parse_pdf(args.pdf)
    if len(source) != 110 or [row["serial"] for row in source] != list(range(1, 111)):
        raise SystemExit(f"PDF parser found {len(source)} sequential products; expected 110")
    if len(PRODUCTS) != 110:
        raise SystemExit(f"Catalog manifest has {len(PRODUCTS)} rows; expected 110")

    catalog_mismatches = []
    catalog_by_serial = {row[0]: row for row in PRODUCTS}
    for row in source:
        serial = row["serial"]
        _, name, hsn, gst, quantity, unit, buying, selling, _category = catalog_by_serial[serial]
        expected = (name, hsn, Decimal(gst), Decimal(quantity), unit.casefold(),
                    Decimal(buying), decimal_or_none(selling))
        actual = (row["name"], row["hsn"], row["gst"], row["quantity"], row["unit"].casefold(),
                  row["buying"], row["selling"])
        if actual != expected:
            catalog_mismatches.append(serial)

    connection = sqlite3.connect(f"file:{args.db.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    db_rows = {row["sku"]: row for row in connection.execute(
        "SELECT id,name,brand,sku,category,unit,pack_size,hsn_code,gst_rate,initial_quantity,stock,"
        "purchase_price_including_gst,selling_price,is_active,source_reference FROM products "
        "WHERE is_active=1 AND sku LIKE 'AAS-%'")}
    movements = connection.execute(
        "SELECT product_id,COUNT(*) AS n FROM stock_movements WHERE movement_type='OPENING_STOCK' GROUP BY product_id"
    ).fetchall()
    opening_movements = {row["product_id"]: row["n"] for row in movements}
    connection.close()

    columns = ["serial", "product_id", "sku", "product_name_pdf", "brand_pdf", "brand_db", "category", "pack_size",
               "hsn_pdf", "hsn_db", "gst_pdf", "gst_db", "opening_quantity_pdf", "opening_quantity_db",
               "buying_price_pdf", "buying_price_db", "buying_unit_pdf", "buying_unit_db",
               "selling_price_pdf", "selling_price_db", "current_stock", "opening_movement_count",
               "pdf_vs_manifest", "db_vs_pdf", "review_flags"]
    output_rows = []
    field_mismatches = []
    for source_row in source:
        serial = source_row["serial"]
        sku = f"AAS-{serial:03d}"
        db_row = db_rows.get(sku)
        source_brand = SOURCE_BRANDS.get(serial)
        expected_category = SOURCE_CATEGORIES.get(serial, DEFAULT_SOURCE_CATEGORY)
        catalog_match = serial not in catalog_mismatches
        flags = []
        if source_row["selling"] is None:
            flags.append("Selling price blank in source; retained blank")
        if source_row["selling"] is not None and source_row["selling"] < source_row["buying"]:
            flags.append("Selling price below buying price in source; retained as written")
        if serial in (7, 13):
            flags.append("Same name and pack as another source row; retained as separate serial/SKU")
        if db_row and db_row["category"] == "Other Agricultural Products":
            flags.append("Category classification uncertain; assigned Other Agricultural Products")
        db_match = False
        if db_row is None:
            field_mismatches.append(serial)
            flags.append("Active product record missing")
        else:
            db_match = (
                db_row["name"] == source_row["name"]
                and db_row["hsn_code"] == source_row["hsn"]
                and decimal_or_none(db_row["gst_rate"]) == source_row["gst"]
                and decimal_or_none(db_row["initial_quantity"]) == source_row["quantity"]
                and db_row["unit"].casefold() == source_row["unit"].casefold()
                and decimal_or_none(db_row["purchase_price_including_gst"]) == source_row["buying"]
                and decimal_or_none(db_row["selling_price"]) == source_row["selling"]
                and db_row["brand"] == source_brand
                and db_row["category"] == expected_category
                and db_row["category"] in VALID_CATEGORIES
                and db_row["source_reference"] is not None
                and opening_movements.get(db_row["id"], 0) == 1
            )
            if not db_match:
                field_mismatches.append(serial)
                flags.append("One or more database fields or opening movement do not match the PDF")
        output_rows.append({
            "serial": serial, "product_id": db_row["id"] if db_row else "", "sku": sku,
            "product_name_pdf": source_row["name"], "brand_pdf": source_brand or "",
            "brand_db": db_row["brand"] if db_row else "",
            "category": db_row["category"] if db_row else "", "pack_size": db_row["pack_size"] if db_row else "",
            "hsn_pdf": source_row["hsn"], "hsn_db": db_row["hsn_code"] if db_row else "",
            "gst_pdf": source_row["gst"], "gst_db": db_row["gst_rate"] if db_row else "",
            "opening_quantity_pdf": source_row["quantity"], "opening_quantity_db": db_row["initial_quantity"] if db_row else "",
            "buying_price_pdf": source_row["buying"], "buying_price_db": db_row["purchase_price_including_gst"] if db_row else "",
            "buying_unit_pdf": source_row["unit"], "buying_unit_db": db_row["unit"] if db_row else "",
            "selling_price_pdf": "" if source_row["selling"] is None else source_row["selling"],
            "selling_price_db": "" if not db_row or db_row["selling_price"] is None else db_row["selling_price"],
            "current_stock": db_row["stock"] if db_row else "",
            "opening_movement_count": opening_movements.get(db_row["id"], 0) if db_row else 0,
            "pdf_vs_manifest": "MATCH" if catalog_match else "MISMATCH",
            "db_vs_pdf": "MATCH" if db_match else "MISMATCH", "review_flags": "; ".join(flags),
        })

    if len(db_rows) != 110:
        field_mismatches.append(-1)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / "product-import-validation.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(output_rows)

    selling_blank = [row["serial"] for row in source if row["selling"] is None]
    below_cost = [row["serial"] for row in source if row["selling"] is not None and row["selling"] < row["buying"]]
    uncertain = [row["serial"] for row in output_rows if row["category"] == "Other Agricultural Products"]
    category_counts: dict[str, int] = {}
    for row in output_rows:
        category_counts[row["category"]] = category_counts.get(row["category"], 0) + 1
    digest = hashlib.sha256(args.pdf.read_bytes()).hexdigest()
    report_path = args.output_dir / "product-import-validation.md"
    report_path.write_text(
        "# Adinath Agri Services Product Import Validation\n\n"
        f"- Source: `{args.pdf.name}` (4 pages)\n"
        f"- Source SHA-256: `{digest}`\n"
        "- Products processed: **110 / 110**\n"
        f"- PDF to catalog manifest: **{110-len(catalog_mismatches)} MATCH**, {len(catalog_mismatches)} mismatches\n"
        f"- PDF to active database: **{110-len(set(field_mismatches))} MATCH**, {len(set(field_mismatches))} mismatches\n"
        "- Opening stock: checked against each product's `initial_quantity`; one opening ledger movement per SKU verified. Current stock is reported separately because sales may change it.\n"
        "- Database scope: only the 110 active `AAS-001` through `AAS-110` source rows; inactive legacy products are not counted or changed.\n\n"
        "## Category counts\n\n" + "\n".join(f"- {name}: {count}" for name, count in sorted(category_counts.items())) +
        "\n\n## Manual review flags\n\n"
        f"- Selling price blank in source and kept blank: rows {', '.join(map(str, selling_blank))}.\n"
        f"- Selling price below buying price in source and retained as written: rows {', '.join(map(str, below_cost))}.\n"
        "- Duplicate-looking source name: rows 7 and 13 are both `Strike (IIL) 500 Gm`; they have distinct source serials, opening quantities and SKUs, so both records remain.\n"
        f"- Category uncertain from source detail and left under Other Agricultural Products: {len(uncertain)} rows ({', '.join(map(str, uncertain))}).\n\n"
        f"The CSV contains all 110 source values, database comparisons, product IDs, current stock and per-row flags: `{csv_path.name}`.\n",
        encoding="utf-8",
    )
    print(f"PDF rows: {len(source)}; PDF/manifest mismatches: {len(catalog_mismatches)}; database mismatches: {len(set(field_mismatches))}")
    print(f"CSV: {csv_path}")
    print(f"Report: {report_path}")
    return 1 if catalog_mismatches or field_mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
