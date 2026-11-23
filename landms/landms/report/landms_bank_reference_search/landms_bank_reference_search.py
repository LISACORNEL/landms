"""Every record that mentions a bank reference: payments, journals, bank notices, logs, bills, orders and notes."""

import frappe
from frappe import _
from frappe.utils import escape_html, flt

SHORTEST = 6  # fewer characters would match half the system
PER_SOURCE = 100

# (record type, fields searched, date field, amount field, customer field, order field)
SOURCES = (
	("Payment Entry", ("name", "reference_no", "remarks"), "posting_date", "paid_amount", "party", None),
	("Journal Entry", ("cheque_no", "user_remark"), "posting_date", "total_debit", None, None),
	("Bank Transaction", ("reference_number", "description"), "date", "deposit", "party", None),
	(
		"TCB Payment Notification",
		("transaction_id", "reference"),
		"received_at",
		"amount",
		None,
		"sales_order",
	),
	(
		"TCB API Log",
		("transaction_id", "external_reference", "related_ref"),
		"requested_at",
		None,
		None,
		"sales_order",
	),
	(
		"TCB Control Number",
		("control_number", "payment_reference", "payment_entry"),
		"generated_at",
		"amount",
		"customer",
		"sales_order",
	),
	(
		"Payment Receipt Ledger",
		("txn_id", "reference", "ml_reference"),
		"paid_at",
		"amount",
		None,
		"sales_order",
	),
	(
		"Malipopay Collection",
		("control_number", "ml_reference"),
		"activated_at",
		"provider_paid_amount",
		"customer",
		"sales_order",
	),
	(
		"Malipopay Webhook Event",
		("transaction_id", "control_number", "ml_reference"),
		"received_at",
		"cumulative_amount",
		None,
		None,
	),
	(
		"Malipopay External Entry",
		("external_reference", "ml_reference", "control_number"),
		"paid_at",
		None,
		None,
		"sales_order",
	),
	(
		"Sales Order",
		("control_number", "related_control_number", "malipopay_reference"),
		"transaction_date",
		"grand_total",
		"customer",
		"name",
	),
	("Comment", ("content",), "creation", None, None, None),
	("Deleted Document", ("deleted_name",), "creation", None, None, None),
)


def execute(filters=None):
	reference = ((filters or {}).get("reference") or "").strip()
	if len(reference) < SHORTEST:
		frappe.throw(_("Type at least {0} characters of the reference.").format(SHORTEST))
	return _columns(), [row for source in SOURCES for row in _matches(source, reference)]


def _columns():
	return [
		{"fieldname": "record_type", "label": _("Record Type"), "fieldtype": "Data", "width": 190},
		{
			"fieldname": "record",
			"label": _("Record"),
			"fieldtype": "Dynamic Link",
			"options": "record_type",
			"width": 210,
		},
		{"fieldname": "state", "label": _("State"), "fieldtype": "Data", "width": 120},
		{"fieldname": "date", "label": _("Date"), "fieldtype": "Datetime", "width": 160},
		{"fieldname": "amount", "label": _("Amount"), "fieldtype": "Currency", "width": 130},
		{"fieldname": "customer", "label": _("Customer"), "fieldtype": "Data", "width": 180},
		{
			"fieldname": "sales_order",
			"label": _("Sales Order"),
			"fieldtype": "Link",
			"options": "Sales Order",
			"width": 170,
		},
		{"fieldname": "found_in", "label": _("Found In"), "fieldtype": "Data", "width": 150},
		{"fieldname": "about", "label": _("About"), "fieldtype": "Data", "width": 220},
	]


def _matches(source, reference):
	"""The records of one type that mention the reference, as report rows."""
	doctype, searched, date_field, amount_field, customer_field, order_field = source
	if not frappe.db.exists("DocType", doctype):
		return []
	meta = frappe.get_meta(doctype)
	searched = [f for f in searched if f == "name" or meta.has_field(f)]
	if not searched:
		return []
	wanted = {f for f in ("name", date_field, amount_field, customer_field, order_field) if f}
	wanted |= {f for f in ("docstatus", "status", "reference_doctype", "reference_name", "deleted_doctype")}
	fields = [
		f
		for f in wanted | set(searched)
		if f == "name" or f == "creation" or f == "docstatus" or meta.has_field(f)
	]
	pattern = f"%{reference}%"
	rows = frappe.get_all(
		doctype,
		or_filters=[[doctype, f, "like", pattern] for f in searched],
		fields=fields,
		order_by="creation desc",
		limit=PER_SOURCE,
	)
	return [
		_row(doctype, r, searched, reference, date_field, amount_field, customer_field, order_field)
		for r in rows
	]


def _row(doctype, r, searched, reference, date_field, amount_field, customer_field, order_field):
	"""One report row; text from the records is escaped, and log contents are never shown."""
	needle = reference.lower()
	found_in = [
		frappe.get_meta(doctype).get_label(f) if f != "name" else _("Name")
		for f in searched
		if needle in str(r.get(f) or "").lower()
	]
	return {
		"record_type": doctype,
		"record": r.name,
		"state": _state(doctype, r),
		"date": r.get(date_field),
		"amount": flt(r.get(amount_field)) if amount_field else None,
		"customer": escape_html(str(r.get(customer_field) or "")) if customer_field else "",
		"sales_order": r.get(order_field) if order_field else None,
		"found_in": ", ".join(found_in),
		"about": escape_html(_about(doctype, r)),
	}


def _state(doctype, r):
	if doctype == "Deleted Document":
		return _("Deleted")
	if doctype == "Comment":
		return _("Note")
	if r.get("docstatus") == 2:
		return _("Cancelled")
	return r.get("status") or {0: _("Draft"), 1: _("Submitted")}.get(r.get("docstatus"), "")


def _about(doctype, r):
	"""Which record a note or a deleted record belongs to."""
	if doctype == "Comment":
		return f"{r.get('reference_doctype') or ''} {r.get('reference_name') or ''}".strip()
	if doctype == "Deleted Document":
		return r.get("deleted_doctype") or ""
	return ""
