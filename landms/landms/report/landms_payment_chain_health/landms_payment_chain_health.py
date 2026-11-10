"""Payment chain health (PB-6): zero rows means every plot payment's chain of steps is complete.

A TCB payment sets off a chain: the payment reaches the ledger, the government share is posted,
the order shows what the invoice was paid, and a fully paid order has a Completed contract. In
Aug-Sep 2026 chains broke halfway (a payment with no ledger rows, a share never posted) and went
unseen for weeks, because the bank check only asks "does a payment with this reference exist?".
Each check below lists the broken chains, one row each; it reads and never changes anything.
"""

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.query_builder.functions import Abs

# the four checks; translated when shown, never at import (each user sees their own language)
NO_LEDGER = "No ledger rows"
NO_SHARE = "No government share"
ORDER_VS_INVOICE = "Order and invoice disagree"
NOT_COMPLETED = "Paid, not Completed"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	rows = [
		*_payments_without_ledger(filters),
		*_payments_without_government_share(filters),
		*_orders_not_matching_invoice(filters),
		*_paid_but_not_completed(filters),
	]
	return _columns(), rows, None, None, _summary(rows)


def _columns():
	return [
		{"fieldname": "check", "label": _("Problem"), "fieldtype": "Data", "width": 190},
		{
			"fieldname": "document_type",
			"label": _("Type"),
			"fieldtype": "Link",
			"options": "DocType",
			"width": 120,
		},
		{
			"fieldname": "document",
			"label": _("Document"),
			"fieldtype": "Dynamic Link",
			"options": "document_type",
			"width": 190,
		},
		{
			"fieldname": "customer",
			"label": _("Customer"),
			"fieldtype": "Link",
			"options": "Customer",
			"width": 200,
		},
		{"fieldname": "amount", "label": _("Amount"), "fieldtype": "Currency", "width": 130},
		{"fieldname": "detail", "label": _("Detail"), "fieldtype": "Data", "width": 300},
		{"fieldname": "date", "label": _("Date"), "fieldtype": "Date", "width": 100},
		{
			"fieldname": "land_acquisition",
			"label": _("Project"),
			"fieldtype": "Link",
			"options": "Land Acquisition",
			"width": 130,
		},
	]


def _payments_without_ledger(filters):
	"""A submitted plot payment with no live ledger row: money at the bank, invisible in the books."""
	pe, gl = DocType("Payment Entry"), DocType("GL Entry")
	query = (
		frappe.qb.from_(pe)
		.left_join(gl)
		.on((gl.voucher_type == "Payment Entry") & (gl.voucher_no == pe.name) & (gl.is_cancelled == 0))
		.select(pe.name, pe.party, pe.paid_amount, pe.posting_date, pe.land_acquisition)
		.where(
			(pe.docstatus == 1)
			& (pe.land_acquisition.isnotnull())
			& (pe.land_acquisition != "")
			& gl.name.isnull()
		)
	)
	if filters.land_acquisition:
		query = query.where(pe.land_acquisition == filters.land_acquisition)
	return [
		_row(
			NO_LEDGER,
			"Payment Entry",
			r.name,
			r.party,
			r.paid_amount,
			_("Submitted, no ledger rows"),
			r.posting_date,
			r.land_acquisition,
		)
		for r in query.run(as_dict=True)
	]


def _payments_without_government_share(filters):
	"""A payment on a plot invoice of a project with a government share, with no share journal."""
	pe, ref, si = DocType("Payment Entry"), DocType("Payment Entry Reference"), DocType("Sales Invoice")
	la, je = DocType("Land Acquisition"), DocType("Journal Entry")
	# the share journal's link to its payment has no index: read it once (one pass over the journals)
	# rather than joining it per payment, which takes minutes on live
	shared = set(
		frappe.qb.from_(je)
		.select(je.lms_payment_entry)
		.where((je.docstatus == 1) & je.lms_payment_entry.isnotnull())
		.distinct()
		.run(pluck=True)
	)
	query = (
		frappe.qb.from_(pe)
		.join(ref)
		.on(ref.parent == pe.name)
		.join(si)
		.on((ref.reference_doctype == "Sales Invoice") & (si.name == ref.reference_name))
		.join(la)
		.on(la.name == si.land_acquisition)
		.select(
			pe.name,
			pe.party,
			pe.paid_amount,
			pe.posting_date,
			si.land_acquisition,
			la.government_share_percent,
		)
		.where(
			(pe.docstatus == 1)
			& (pe.payment_type == "Receive")
			& (si.is_plot_sale_invoice == 1)
			& (ref.allocated_amount > 0)
			& (la.government_share_percent > 0)
		)
		.distinct()
	)
	if filters.land_acquisition:
		query = query.where(si.land_acquisition == filters.land_acquisition)
	missing = [r for r in query.run(as_dict=True) if r.name not in shared]
	return [
		_row(
			NO_SHARE,
			"Payment Entry",
			r.name,
			r.party,
			r.paid_amount,
			_("No share journal ({0}%)").format(r.government_share_percent),
			r.posting_date,
			r.land_acquisition,
		)
		for r in missing
	]


def _orders_not_matching_invoice(filters):
	"""An open plot order whose 'paid' differs from what its invoice was paid, by more than 1."""
	so, si = DocType("Sales Order"), DocType("Sales Invoice")
	invoice_paid = si.grand_total - si.outstanding_amount
	query = (
		frappe.qb.from_(so)
		.join(si)
		.on(si.name == so.plot_sales_invoice)
		.select(
			so.name,
			so.customer,
			so.advance_paid,
			so.transaction_date,
			so.land_acquisition,
			invoice_paid.as_("invoice_paid"),
		)
		.where(
			(so.docstatus == 1)
			& so.status.notin(["Closed", "Cancelled"])
			& (Abs(so.advance_paid - invoice_paid) > 1)
		)
	)
	if filters.land_acquisition:
		query = query.where(so.land_acquisition == filters.land_acquisition)
	return [
		_row(
			ORDER_VS_INVOICE,
			"Sales Order",
			r.name,
			r.customer,
			r.advance_paid,
			_("Order paid {0}, invoice paid {1}").format(
				frappe.format(r.advance_paid, "Currency"), frappe.format(r.invoice_paid, "Currency")
			),
			r.transaction_date,
			r.land_acquisition,
		)
		for r in query.run(as_dict=True)
	]


def _paid_but_not_completed(filters):
	"""An open plot order whose invoice is fully paid but whose contract is missing or not Completed."""
	so, si, contract = DocType("Sales Order"), DocType("Sales Invoice"), DocType("Plot Contract")
	query = (
		frappe.qb.from_(so)
		.join(si)
		.on((si.name == so.plot_sales_invoice) & (si.docstatus == 1))
		.left_join(contract)
		.on(contract.name == so.plot_contract)
		.select(
			so.name,
			so.customer,
			si.grand_total,
			so.transaction_date,
			so.land_acquisition,
			contract.contract_status,
		)
		.where(
			(so.docstatus == 1)
			& so.status.notin(["Closed", "Cancelled"])
			& (si.outstanding_amount <= 0)
			& (contract.name.isnull() | (contract.contract_status != "Completed"))
		)
	)
	if filters.land_acquisition:
		query = query.where(so.land_acquisition == filters.land_acquisition)
	return [
		_row(
			NOT_COMPLETED,
			"Sales Order",
			r.name,
			r.customer,
			r.grand_total,
			_("Contract: {0}").format(r.contract_status or _("none")),
			r.transaction_date,
			r.land_acquisition,
		)
		for r in query.run(as_dict=True)
	]


def _row(check, document_type, document, customer, amount, detail, date, land_acquisition):
	return {
		"check": _(check),
		"document_type": document_type,
		"document": document,
		"customer": customer,
		"amount": amount,
		"detail": detail,
		"date": date,
		"land_acquisition": land_acquisition,
	}


def _summary(rows):
	"""One card per check: green at zero."""
	cards = []
	for check in (NO_LEDGER, NO_SHARE, ORDER_VS_INVOICE, NOT_COMPLETED):
		count = sum(1 for r in rows if r["check"] == _(check))
		cards.append(
			{
				"label": _(check),
				"value": count,
				"datatype": "Int",
				"indicator": "Green" if count == 0 else "Red",
			}
		)
	return cards
