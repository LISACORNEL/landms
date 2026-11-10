"""What a project's land has cost: committed, billed and paid, per supplier and in total (LA-2).

The single source of truth for the purchase order / invoice / payment roll-ups per supplier. The project
form shows them (refresh_cost_summary); purchases, journals and payments that carry the project refresh
them (landms/overrides/update_project_costs.py). Moved unchanged from land_acquisition.py.
"""

import frappe
from frappe.query_builder import DocType, Order
from frappe.query_builder.functions import Abs, Coalesce, Sum
from frappe.utils import flt


@frappe.whitelist(methods=["POST"])
def refresh_cost_summary(land_acquisition: str):
	"""The project form's cost panel: recompute, save and return the cost summary.

	Answers anyone who may open the project and refuses everyone else. Read is
	enough: on live most roles that open a project may only read it.
	"""
	frappe.has_permission("Land Acquisition", "read", land_acquisition, throw=True)
	return sync_land_acquisition_cost_summary(land_acquisition)


def sync_land_acquisition_cost_summary(land_acquisition):
	"""Recompute the cost summary and persist totals on the Land Acquisition.

	Not a web door: the purchase, journal and payment hooks call it for whoever
	submits those documents, who may have no rights on the project. The form
	goes through refresh_cost_summary.
	"""
	if not land_acquisition or not frappe.db.exists("Land Acquisition", land_acquisition):
		return {}

	summary = get_land_acquisition_cost_summary(land_acquisition)
	t = summary["totals"]
	frappe.db.set_value(
		"Land Acquisition",
		land_acquisition,
		{
			"acquisition_cost_tzs": t["acquisition_cost_tzs"],
			"cost_per_sqm_tzs": t["cost_per_sqm_tzs"],
			"total_committed_tzs": t["committed"],
			"total_paid_tzs": t["paid"],
			"total_outstanding_tzs": t["outstanding"],
			"total_unbilled_po_tzs": t["unbilled_po"],
			"je_billed_tzs": t["je_billed"],
		},
		update_modified=False,
	)
	return summary


# Not a web door: it returns every seller and supplier amount, and only this
# module's own code reads it (through the doors above, which check permission).
def get_land_acquisition_cost_summary(land_acquisition):
	"""Return the consolidated PO/PI/PE rollup for one Land Acquisition.

	Shape:
	    {
	      "land_acquisition": str,
	      "total_area_sqm":   float,
	      "lud_account":      str,
	      "sellers":          [supplier_row, ...],
	      "others":           [supplier_row, ...],
	      "totals":           { ...numbers... },
	    }

	supplier_row:
	    {
	      "supplier": str, "supplier_name": str, "is_land_seller": int,
	      "committed":   float,  # PO total tagged to this LA
	      "billed":      float,  # PI total tagged to this LA hitting LUD
	      "paid":        float,  # PE allocations against those PIs
	      "outstanding": float,  # billed - paid
	      "unbilled_po": float,  # max(committed - billed, 0)
	      "po_docs": [...], "pi_docs": [...], "pe_docs": [...],
	    }
	"""
	if not land_acquisition or not frappe.db.exists("Land Acquisition", land_acquisition):
		return _empty_summary(land_acquisition)

	la = frappe.db.get_value(
		"Land Acquisition",
		land_acquisition,
		["company", "total_area_sqm"],
		as_dict=True,
	)
	lud_account = _get_lud_account(la.company)

	po_rows = _fetch_committed(land_acquisition)
	pi_rows = _fetch_billed(land_acquisition, lud_account)
	pe_rows, paid_per_pi = _fetch_paid([r["pi"] for r in pi_rows])
	advance_rows = _fetch_paid_advance(land_acquisition)

	# Annotate per-PI paid/outstanding for the drill-down view
	for row in pi_rows:
		row["paid"] = flt(paid_per_pi.get(row["pi"], 0))
		row["outstanding"] = flt(row["amount"]) - row["paid"]

	suppliers = _build_supplier_rollup(po_rows, pi_rows, pe_rows, advance_rows)
	sellers = [s for s in suppliers if s["is_land_seller"]]
	others = [s for s in suppliers if not s["is_land_seller"]]
	totals = _compute_totals(sellers, others, flt(la.total_area_sqm))

	plot_inv_account = frappe.db.get_single_value("LandMS Settings", "plot_inventory_account")
	je_billed = _fetch_billed_from_je(land_acquisition, lud_account, plot_inv_account)
	je_rows = _fetch_je_rows(land_acquisition, lud_account, plot_inv_account)
	totals["je_billed"] = je_billed
	if je_billed:
		area = flt(la.total_area_sqm)
		totals["acquisition_cost_tzs"] = flt(totals["acquisition_cost_tzs"]) + je_billed
		totals["billed"] = flt(totals["billed"]) + je_billed
		totals["cost_per_sqm_tzs"] = (totals["acquisition_cost_tzs"] / area) if area else 0.0

	return {
		"land_acquisition": land_acquisition,
		"total_area_sqm": flt(la.total_area_sqm),
		"lud_account": lud_account,
		"sellers": sellers,
		"others": others,
		"totals": totals,
		"je_rows": je_rows,
	}


def _get_lud_account(company):
	"""Resolve the Land Under Development account for the given company."""
	account = frappe.db.get_single_value("LandMS Settings", "land_under_development_account")
	if account:
		return account
	# Fallback by account number on the company
	return frappe.db.get_value(
		"Account",
		{"company": company, "account_number": "1411"},
		"name",
	)


def _fetch_committed(la_name):
	"""Submitted Purchase Order items tagged to this Land Acquisition."""
	if not frappe.db.has_column("Purchase Order Item", "land_acquisition"):
		return []
	poi, po = DocType("Purchase Order Item"), DocType("Purchase Order")
	amount = Sum(poi.base_net_amount)
	return (
		frappe.qb.from_(poi)
		.join(po)
		.on(po.name == poi.parent)
		.select(
			po.name.as_("po"),
			po.supplier.as_("supplier"),
			po.transaction_date.as_("date"),
			po.status.as_("status"),
			amount.as_("amount"),
		)
		.where((poi.land_acquisition == la_name) & (po.docstatus == 1))
		.groupby(po.name)
		.having(Abs(amount) > 0.0001)
		.orderby(po.transaction_date, order=Order.desc)
		.orderby(po.creation, order=Order.desc)
		.run(as_dict=True)
	)


def _fetch_billed(la_name, lud_account):
	"""Submitted Purchase Invoice items tagged to this LA hitting the LUD account."""
	if not frappe.db.has_column("Purchase Invoice Item", "land_acquisition"):
		return []
	if not lud_account:
		return []
	pii, pi = DocType("Purchase Invoice Item"), DocType("Purchase Invoice")
	amount = Sum(pii.base_net_amount)
	return (
		frappe.qb.from_(pii)
		.join(pi)
		.on(pi.name == pii.parent)
		.select(
			pi.name.as_("pi"),
			pi.supplier.as_("supplier"),
			pi.posting_date.as_("date"),
			pi.status.as_("status"),
			amount.as_("amount"),
		)
		.where((pii.land_acquisition == la_name) & (pi.docstatus == 1) & (pii.expense_account == lud_account))
		.groupby(pi.name)
		.having(Abs(amount) > 0.0001)
		.orderby(pi.posting_date, order=Order.desc)
		.orderby(pi.creation, order=Order.desc)
		.run(as_dict=True)
	)


def _land_journal_lines(la_name, accounts):
	"""Submitted journal lines tagged to this LA that debit one of the land cost accounts."""
	jea, je = DocType("Journal Entry Account"), DocType("Journal Entry")
	query = (
		frappe.qb.from_(jea)
		.join(je)
		.on(je.name == jea.parent)
		.where(
			(jea.land_acquisition == la_name)
			& (je.docstatus == 1)
			& jea.account.isin(accounts)
			& (jea.debit_in_account_currency > 0)
		)
	)
	return query, jea, je


def _fetch_je_rows(la_name, lud_account, plot_inv_account):
	"""Return individual submitted JE rows tagged to this LA on land cost accounts."""
	accounts = tuple(a for a in [lud_account, plot_inv_account] if a)
	if not accounts:
		return []
	query, jea, je = _land_journal_lines(la_name, accounts)
	return (
		query.select(
			je.name.as_("je_name"),
			je.posting_date,
			je.user_remark,
			jea.debit_in_account_currency.as_("amount"),
		)
		.orderby(je.posting_date, order=Order.desc)
		.orderby(je.name)
		.run(as_dict=True)
	)


def _fetch_billed_from_je(la_name, lud_account, plot_inv_account):
	"""Sum debit amounts from submitted JEs tagged with this LA on land cost accounts."""
	accounts = tuple(a for a in [lud_account, plot_inv_account] if a)
	if not accounts:
		return 0.0
	query, jea, _je = _land_journal_lines(la_name, accounts)
	result = query.select(Coalesce(Sum(jea.debit_in_account_currency), 0)).run()
	return flt(result[0][0]) if result else 0.0


def _fetch_paid(pi_names):
	"""Payment Entry allocations against the given PI names.

	Returns (pe_rows, {pi_name: total_paid}).
	"""
	if not pi_names:
		return [], {}
	per, pe = DocType("Payment Entry Reference"), DocType("Payment Entry")
	rows = (
		frappe.qb.from_(per)
		.join(pe)
		.on(pe.name == per.parent)
		.select(
			pe.name.as_("pe"),
			pe.party.as_("supplier"),
			pe.posting_date.as_("date"),
			per.reference_name.as_("pi"),
			Sum(per.allocated_amount).as_("amount"),
		)
		.where(
			(per.reference_doctype == "Purchase Invoice")
			& per.reference_name.isin(tuple(pi_names))
			& (pe.docstatus == 1)
		)
		.groupby(pe.name, per.reference_name)
		.orderby(pe.posting_date, order=Order.desc)
		.orderby(pe.creation, order=Order.desc)
		.run(as_dict=True)
	)
	paid_per_pi = {}
	for r in rows:
		paid_per_pi[r["pi"]] = paid_per_pi.get(r["pi"], 0) + flt(r["amount"])
	return rows, paid_per_pi


def _fetch_paid_advance(la_name):
	"""Payment Entry allocations made directly against Purchase Orders tagged
	to this Land Acquisition (i.e. supplier advances paid before any PI exists).

	A single PO can hold items for multiple Land Acquisitions, so each
	allocation is pro-rated by this LA's share of the PO total. The result
	looks the same shape as `_fetch_paid` rows so the rollup can consume both.
	"""
	if not frappe.db.has_column("Purchase Order Item", "land_acquisition"):
		return []
	poi = DocType("Purchase Order Item")
	# this LA's share of each of its purchase orders, and each order's total
	la_share = dict(
		frappe.qb.from_(poi)
		.select(poi.parent, Sum(poi.base_net_amount))
		.where(poi.land_acquisition == la_name)
		.groupby(poi.parent)
		.run()
	)
	if not la_share:
		return []
	po_total = dict(
		frappe.qb.from_(poi)
		.select(poi.parent, Sum(poi.base_net_amount))
		.where(poi.parent.isin(tuple(la_share)))
		.groupby(poi.parent)
		.run()
	)
	per, pe = DocType("Payment Entry Reference"), DocType("Payment Entry")
	rows = (
		frappe.qb.from_(per)
		.join(pe)
		.on(pe.name == per.parent)
		.select(
			pe.name.as_("pe"),
			pe.party.as_("supplier"),
			pe.posting_date.as_("date"),
			per.reference_name.as_("po"),
			per.allocated_amount.as_("allocated"),
		)
		.where(
			(per.reference_doctype == "Purchase Order")
			& (pe.docstatus == 1)
			& per.reference_name.isin(tuple(la_share))
		)
		.orderby(pe.posting_date, order=Order.desc)
		.orderby(pe.creation, order=Order.desc)
		.run(as_dict=True)
	)

	result = []
	for r in rows:
		total = flt(po_total.get(r["po"]))
		ratio = (flt(la_share.get(r["po"])) / total) if total else 0.0
		amount = flt(r["allocated"]) * ratio
		if abs(amount) <= 0.0001:
			continue
		result.append(
			{
				"pe": r["pe"],
				"supplier": r["supplier"],
				"date": r["date"],
				"po": r["po"],
				"amount": amount,
			}
		)
	return result


def _build_supplier_rollup(po_rows, pi_rows, pe_rows, advance_rows=None):
	"""Aggregate per-supplier metrics from the three doc-type row sets."""
	advance_rows = advance_rows or []
	supplier_names = (
		{r["supplier"] for r in po_rows}
		| {r["supplier"] for r in pi_rows}
		| {r["supplier"] for r in pe_rows}
		| {r["supplier"] for r in advance_rows}
	)
	supplier_meta = {}
	if supplier_names:
		meta_rows = frappe.db.get_all(
			"Supplier",
			filters={"name": ("in", list(supplier_names))},
			fields=["name", "supplier_name", "is_land_seller"],
		)
		supplier_meta = {r.name: r for r in meta_rows}

	suppliers = {}

	def _row(name):
		if name not in suppliers:
			meta = supplier_meta.get(name) or frappe._dict()
			suppliers[name] = {
				"supplier": name,
				"supplier_name": meta.get("supplier_name") or name,
				"is_land_seller": int(meta.get("is_land_seller") or 0),
				"committed": 0.0,
				"billed": 0.0,
				"paid": 0.0,
				"outstanding": 0.0,
				"unbilled_po": 0.0,
				"po_docs": [],
				"pi_docs": [],
				"pe_docs": [],
			}
		return suppliers[name]

	for r in po_rows:
		s = _row(r["supplier"])
		s["committed"] += flt(r["amount"])
		s["po_docs"].append(
			{
				"name": r["po"],
				"amount": flt(r["amount"]),
				"date": str(r["date"]) if r["date"] else None,
				"status": r["status"],
			}
		)

	for r in pi_rows:
		s = _row(r["supplier"])
		s["billed"] += flt(r["amount"])
		s["paid"] += flt(r["paid"])
		s["pi_docs"].append(
			{
				"name": r["pi"],
				"amount": flt(r["amount"]),
				"paid": flt(r["paid"]),
				"outstanding": flt(r["outstanding"]),
				"date": str(r["date"]) if r["date"] else None,
				"status": r["status"],
			}
		)

	for r in pe_rows:
		s = _row(r["supplier"])
		s["pe_docs"].append(
			{
				"name": r["pe"],
				"amount": flt(r["amount"]),
				"date": str(r["date"]) if r["date"] else None,
				"pi": r["pi"],
				"against": "pi",
			}
		)

	for r in advance_rows:
		s = _row(r["supplier"])
		s["paid"] += flt(r["amount"])
		s["pe_docs"].append(
			{
				"name": r["pe"],
				"amount": flt(r["amount"]),
				"date": str(r["date"]) if r["date"] else None,
				"po": r["po"],
				"against": "po",
			}
		)

	for s in suppliers.values():
		s["outstanding"] = max(s["billed"] - s["paid"], 0.0)
		s["unbilled_po"] = max(s["committed"] - s["billed"], 0.0)

	return sorted(
		suppliers.values(),
		key=lambda s: (-s["is_land_seller"], -(s["committed"] + s["billed"])),
	)


def _compute_totals(sellers, others, total_area_sqm):
	def _sum(rows, key):
		return sum(flt(r[key]) for r in rows)

	seller_billed = _sum(sellers, "billed")
	other_billed = _sum(others, "billed")
	acquisition_cost = seller_billed + other_billed
	cost_per_sqm = (acquisition_cost / total_area_sqm) if total_area_sqm else 0.0

	return {
		# split by supplier type
		"seller_committed": _sum(sellers, "committed"),
		"seller_billed": seller_billed,
		"seller_paid": _sum(sellers, "paid"),
		"seller_outstanding": _sum(sellers, "outstanding"),
		"seller_unbilled_po": _sum(sellers, "unbilled_po"),
		"other_committed": _sum(others, "committed"),
		"other_billed": other_billed,
		"other_paid": _sum(others, "paid"),
		"other_outstanding": _sum(others, "outstanding"),
		"other_unbilled_po": _sum(others, "unbilled_po"),
		# grand totals
		"committed": _sum(sellers, "committed") + _sum(others, "committed"),
		"billed": acquisition_cost,
		"paid": _sum(sellers, "paid") + _sum(others, "paid"),
		"outstanding": _sum(sellers, "outstanding") + _sum(others, "outstanding"),
		"unbilled_po": _sum(sellers, "unbilled_po") + _sum(others, "unbilled_po"),
		# cost-basis used by Plot Master allocation
		"acquisition_cost_tzs": acquisition_cost,
		"cost_per_sqm_tzs": cost_per_sqm,
	}


def _empty_summary(la_name):
	return {
		"land_acquisition": la_name,
		"total_area_sqm": 0.0,
		"lud_account": None,
		"sellers": [],
		"others": [],
		"totals": {
			k: 0.0
			for k in (
				"seller_committed",
				"seller_billed",
				"seller_paid",
				"seller_outstanding",
				"seller_unbilled_po",
				"other_committed",
				"other_billed",
				"other_paid",
				"other_outstanding",
				"other_unbilled_po",
				"committed",
				"billed",
				"paid",
				"outstanding",
				"unbilled_po",
				"acquisition_cost_tzs",
				"cost_per_sqm_tzs",
				"je_billed",
			)
		},
	}
