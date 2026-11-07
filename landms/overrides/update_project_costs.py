"""When a purchase order, purchase invoice, journal or payment carrying a project is saved, keep that
project's costs up to date (LA-2). Wired in hooks.py. Moved unchanged from land_acquisition.py.
"""

import frappe

from landms.landms.doctype.land_acquisition.cost_totals import sync_land_acquisition_cost_summary


def _sync_many(la_names):
	for name in {n for n in (la_names or []) if n}:
		sync_land_acquisition_cost_summary(name)


def sync_costs_from_purchase_order(doc, method=None):
	_sync_many({row.land_acquisition for row in (doc.get("items") or []) if row.get("land_acquisition")})


def sync_costs_from_purchase_invoice(doc, method=None):
	_sync_many({row.land_acquisition for row in (doc.get("items") or []) if row.get("land_acquisition")})


def sync_costs_from_journal_entry(doc, method=None):
	_sync_many({row.land_acquisition for row in (doc.get("accounts") or []) if row.get("land_acquisition")})


def autoset_land_acquisition_on_payment_entry(doc, method=None):
	"""Tag a Payment Entry with the LA dimension by following its references.

	Walks both Purchase Invoice and Purchase Order references — the latter
	catches advance payments made before any invoice exists. Only sets the
	dimension on the PE header when all referenced docs resolve to the same
	LA. Mixed-LA payments are left untagged so the operator reviews them.
	"""
	if doc.get("payment_type") != "Pay" or doc.get("party_type") != "Supplier":
		return
	if doc.get("land_acquisition"):
		return
	la_names = set()
	for r in doc.get("references") or []:
		if r.reference_doctype == "Purchase Invoice":
			child_dt = "Purchase Invoice Item"
		elif r.reference_doctype == "Purchase Order":
			child_dt = "Purchase Order Item"
		else:
			continue
		item_rows = frappe.db.get_all(
			child_dt,
			filters={"parent": r.reference_name},
			fields=["land_acquisition"],
		)
		for row in item_rows:
			if row.land_acquisition:
				la_names.add(row.land_acquisition)
	if len(la_names) == 1:
		doc.land_acquisition = la_names.pop()


def sync_costs_from_payment_entry(doc, method=None):
	"""Refresh LA cost summaries when a PE is submitted or cancelled.

	A single PE can settle PIs and/or pay advances against POs across multiple
	Land Acquisitions, so we walk every reference (both kinds) and resync each
	affected LA.
	"""
	la_names = set()
	if doc.get("land_acquisition"):
		la_names.add(doc.land_acquisition)
	for r in doc.get("references") or []:
		if r.reference_doctype == "Purchase Invoice":
			child_dt = "Purchase Invoice Item"
		elif r.reference_doctype == "Purchase Order":
			child_dt = "Purchase Order Item"
		else:
			continue
		item_rows = frappe.db.get_all(
			child_dt,
			filters={"parent": r.reference_name},
			fields=["land_acquisition"],
		)
		for row in item_rows:
			if row.land_acquisition:
				la_names.add(row.land_acquisition)
	_sync_many(la_names)


def set_land_acquisition_expense_account(doc, method=None):
	"""Auto-fill land_acquisition on item rows from the header, then set
	expense_account to Land Under Development on all tagged items.

	ERPNext's accounting dimension already copies land_acquisition from header
	to items. This hook is a safety net — it ensures the expense_account is
	always Land Under Development regardless of the item's default, so survey
	fees, legal fees, and land price all capitalise correctly.
	"""
	header_la = doc.get("land_acquisition")

	# Propagate header land_acquisition to any item rows that missed it
	if header_la:
		for item in doc.get("items") or []:
			if not item.get("land_acquisition"):
				item.land_acquisition = header_la

	has_la_items = any(row.get("land_acquisition") for row in (doc.get("items") or []))
	if not has_la_items:
		return

	land_account = frappe.db.get_single_value("LandMS Settings", "land_under_development_account")
	if not land_account:
		return

	cost_center = frappe.db.get_single_value("LandMS Settings", "cost_center")

	for item in doc.get("items") or []:
		if item.get("land_acquisition"):
			item.expense_account = land_account
			if cost_center:
				item.cost_center = cost_center
