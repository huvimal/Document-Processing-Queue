"""Strict invoice schema, arithmetic checks and reviewed CSV export."""
import csv
import io
from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt

Money = Annotated[StrictInt, Field(ge=0, le=1_000_000_000_000)]
Quantity = Annotated[StrictInt, Field(ge=1, le=100_000)]
Text = Annotated[str, Field(min_length=1, max_length=160)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Line(Record):
    description: Text | None
    quantity: Quantity | None
    unit_price: Money | None
    amount: Money | None


class Invoice(Record):
    seller: Text | None
    invoice_no: Text | None
    invoice_date: date | None
    lines: list[Line] = Field(max_length=50)
    subtotal: Money | None
    tax: Money | None
    total: Money | None


def issues(invoice):
    errors = []
    for name in ("seller", "invoice_no", "invoice_date", "subtotal", "tax", "total"):
        if getattr(invoice, name) is None:
            errors.append(f"Thiếu trường {name}.")
    if not invoice.lines:
        errors.append("Cần ít nhất một dòng hàng.")
    for number, line in enumerate(invoice.lines, 1):
        if None in (line.description, line.quantity, line.unit_price, line.amount):
            errors.append(f"Dòng {number} thiếu dữ liệu.")
        elif line.quantity * line.unit_price != line.amount:
            errors.append(f"Dòng {number}: số lượng × đơn giá khác thành tiền.")
    if invoice.lines and invoice.subtotal is not None and all(line.amount is not None for line in invoice.lines):
        if sum(line.amount for line in invoice.lines) != invoice.subtotal:
            errors.append("Tổng các dòng khác tạm tính.")
    if None not in (invoice.subtotal, invoice.tax, invoice.total):
        if invoice.subtotal + invoice.tax != invoice.total:
            errors.append("Tạm tính + thuế khác tổng thanh toán.")
    return errors


def export_csv(invoice):
    if issues(invoice):
        raise ValueError("Resolve invoice issues before export.")
    output = io.StringIO(newline="")
    fields = ["seller", "invoice_no", "invoice_date", "description", "quantity",
              "unit_price_vnd", "amount_vnd", "invoice_subtotal_vnd", "invoice_tax_vnd", "invoice_total_vnd"]
    writer = csv.writer(output)
    writer.writerow(fields)

    def safe(text):
        if isinstance(text, str) and (text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n"))):
            return "'" + text
        return text

    for line in invoice.lines:
        writer.writerow([safe(invoice.seller), safe(invoice.invoice_no), invoice.invoice_date.isoformat(),
                         safe(line.description), line.quantity, line.unit_price, line.amount,
                         invoice.subtotal, invoice.tax, invoice.total])
    return output.getvalue().encode("utf-8-sig")
