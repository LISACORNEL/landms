"""Sales Order lifecycle hooks for LandMS plot sales.

The Sales Order is the commercial anchor for the plot sale:

  Plot Application (application fee paid)
    -> Sales Order (submitted, gets TCB control number)
    -> Draft Plot Contract (created immediately from SO)
    -> First advance payment
    -> Single Plot Sales Invoice (created on first advance only)
    -> Plot Contract auto-submits and mirrors SI / PE state

Important business rules from the user / legacy LMS:
  - The application fee is separate; it never becomes part of the plot SI.
  - The plot SI is created only after the first advance is received.
  - All later installments continue against that same single SI.
  - If the first advance is never received before the application validity
    window expires, the application and its Sales Order are auto-cancelled.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, cint, cstr, flt, getdate, today

from landms.elevation import become_administrator, restore_caller
from landms.landms.doctype.plot_master.plot_master import get_plot_item_code
from landms.landms.doctype.plot_master.plot_status import link, set_plot_status

# Register/decline route through the payment-channel router, which resolves the
# channel from the order's Land Acquisition ("Control Numbers Issued By"). For a
# TCB-issued project — every project today — these land in the identical
# landms.tcb functions.
from landms.payments.router import (
	decline_reference_for_sales_order,
	register_reference_on_submit,
)
from landms.tcb import (
	create_or_get_registry,
	generate_control_number,
	is_valid_control_number,
	is_valid_tcb_mobile,
)


@frappe.whitelist()
def get_sales_order_defaults(plot_application):
	"""Return all fields needed to populate a Sales Order from a Plot Application."""
	if not plot_application or not frappe.db.exists("Plot Application", plot_application):
		frappe.throw(f"Plot Application {plot_application} not found.")

	app = frappe.get_doc("Plot Application", plot_application)
	if app.docstatus != 1 or app.status != "Paid":
		frappe.throw(f"Plot Application {app.name} must be Paid. Current status: {app.status}")

	plot = frappe.get_doc("Plot Master", app.plot)
	settings = frappe.get_single("LandMS Settings")

	payment_completion_days = cint(plot.payment_completion_days or 0)
	transaction_date = app.payment_date or today()
	payment_deadline = add_days(transaction_date, payment_completion_days)

	item_row = build_sales_order_item_row(plot, settings.plot_inventory_warehouse, payment_deadline)
	schedule_rows = build_payment_schedule_rows(
		total_amount=flt(plot.selling_price),
		booking_fee_percent=flt(plot.booking_fee_percent),
		transaction_date=transaction_date,
		payment_deadline=payment_deadline,
	)

	return {
		"customer": app.customer,
		"plot": app.plot,
		"land_acquisition": plot.land_acquisition,
		"acquisition_name": plot.acquisition_name,
		"booking_fee_percent": flt(plot.booking_fee_percent),
		"government_share_percent": flt(plot.government_share_percent),
		"payment_completion_days": payment_completion_days,
		"transaction_date": transaction_date,
		"delivery_date": payment_deadline,
		"payment_deadline": payment_deadline,
		"company": settings.company,
		"cost_center": settings.cost_center,
		"set_warehouse": settings.plot_inventory_warehouse,
		"item_row": item_row,
		"schedule_rows": schedule_rows,
	}


def validate_sales_order(doc, method=None):
	if not _is_landms_sales_order(doc):
		return

	settings = frappe.get_single("LandMS Settings")
	application = _get_application(doc, required=False)

	if application:
		doc.plot = application.plot
		doc.customer = application.customer

	plot = _get_plot(doc)
	doc.company = doc.company or settings.company
	doc.land_acquisition = plot.land_acquisition
	doc.acquisition_name = plot.acquisition_name
	doc.booking_fee_percent = flt(plot.booking_fee_percent)
	doc.government_share_percent = flt(plot.government_share_percent)
	doc.payment_completion_days = cint(plot.payment_completion_days or 0)
	doc.transaction_date = doc.transaction_date or (application.payment_date if application else today())
	doc.payment_deadline = add_days(doc.transaction_date, cint(doc.payment_completion_days or 0))
	doc.set_warehouse = doc.set_warehouse or settings.plot_inventory_warehouse
	doc.ignore_default_payment_terms_template = 1

	_block_manual_control_number(doc)

	if application:
		_validate_application_window(application)
		_ensure_single_sales_order_for_application(doc, application)

	_validate_plot_state(plot)
	_validate_customer_mobile_for_tcb(doc)
	_ensure_items(doc, plot, settings)
	_ensure_payment_schedule(doc, plot)


def before_submit_sales_order(doc, method=None):
	"""REGISTER-FIRST GATE for plot Sales Orders.

	Runs in before_submit (not on_submit) so a registration failure can actually
	BLOCK the submit: before_submit fires before Frappe writes docstatus=1, so a
	throw here leaves the SO in Draft. A throw in on_submit could NOT reliably
	block, because create_tcb_api_log commits the docstatus mid-transaction.

	Mints the control number, records the registry row, and registers with TCB. If
	registration fails the submit is aborted with a clear banner and the SO stays
	Draft so the user can retry. Only once the number is live does on_submit go on
	to link the application, create the contract, and move the plot to Pending Advance.
	"""
	if not _is_landms_sales_order(doc):
		return

	control_number = _ensure_control_number(doc)
	_create_registry_row(doc, control_number)
	_ensure_related_control_number(doc)
	_register_with_tcb(doc, control_number)


def submit_sales_order(doc, method=None):
	if not _is_landms_sales_order(doc):
		return

	# The control number was minted and registered in before_submit_sales_order
	# (the register-first gate). If registration had failed the submit would have
	# been blocked, so by the time we reach here the number is live at the bank —
	# or, when TCB was down, on Malipopay (flagged below; a person registers it at TCB later).
	doc.db_set("plot_outstanding_amount", flt(doc.grand_total), update_modified=False)
	doc.plot_outstanding_amount = flt(doc.grand_total)

	_link_application_to_sales_order(doc)
	contract_name = _ensure_draft_plot_contract(doc)
	if contract_name and doc.get("plot_contract") != contract_name:
		doc.db_set("plot_contract", contract_name, update_modified=False)
		doc.plot_contract = contract_name
	_mark_plot_pending_advance(doc)

	if doc.flags.get("landms_issuer_pending"):
		from landms.payments import tcb_catch_up

		tcb_catch_up.add(
			"Register",
			doc.name,
			doc.control_number,
			"Sold through Malipopay while TCB was not answering; the number still has to be set up at TCB.",
		)
		doc.add_comment(
			"Info",
			(
				f"Submitted while TCB was not answering: payment number {doc.control_number} is not set up at "
				f"TCB yet. The customer can pay through Malipopay meanwhile. The TCB step is listed under "
				f"Waiting for TCB and is finished there once TCB is back."
			),
		)


def before_cancel_sales_order(doc, method=None):
	"""Clean up links + forfeit non-refundable payments before Frappe's link-check.

	Frappe v15 blocks cancellation if any submittable doc (even in Draft state)
	has a Link field pointing to the Sales Order. Plot Contract and Plot
	Application both qualify, so we:
	  - Post forfeiture JE (if the contract is still Draft and had payments)
	    while the contract still exists, since we read its total_paid.
	  - Clear the Plot Application backlink so its submitted-state link doesn't
	    block SO cancel.
	  - Delete the draft Plot Contract.
	  - When forfeiture JE is in play the Plot SI is deliberately left submitted
	    as an audit trail; tell Frappe to skip the SI link check so SO cancel
	    is not blocked (mirrors the termination flow).
	"""
	if not _is_landms_sales_order(doc):
		return

	_block_cancel_if_paid(doc)

	# DECLINE FIRST — the TCB decline is the gate. It runs here in before_cancel
	# (not in on_cancel) for two reasons: (1) the forfeiture JE below must not post
	# unless the number is dead first, and (2) before_cancel runs BEFORE Frappe
	# writes docstatus=2, so a throw here cleanly aborts the cancel. A throw in
	# on_cancel could NOT, because create_tcb_api_log commits the docstatus mid-way.
	# An already-declined number returns ok=True, so retries pass straight through.
	control_number = cstr(doc.get("control_number") or "").strip()
	if control_number:
		result = decline_reference_for_sales_order(doc.name, control_number)
		if not result.get("ok"):
			frappe.throw(
				f"This order could not be cancelled yet because the bank could not be "
				f"reached to release its payment number. Nothing has been changed on "
				f"this order. Please try cancelling it again after a while. "
				f"(Ref {control_number}: {result.get('message')})"
			)

	_post_draft_contract_forfeiture_je(doc)
	_clear_application_sales_order_link(doc)
	_delete_draft_plot_contract(doc)
	if doc.get("forfeiture_entry"):
		doc.flags.ignore_links = True


def cancel_sales_order(doc, method=None):
	if not _is_landms_sales_order(doc):
		return

	# The paid-check and the decline-first gate now run in before_cancel_sales_order
	# (which fires before this hook and before Frappe writes docstatus=2). By the
	# time we reach here the number is already declined, so we only finish cleanup.
	_clear_application_sales_order_link(doc)
	_cancel_unpaid_plot_sales_invoice(doc)
	_delete_draft_plot_contract(doc)
	_cancel_plot_application(doc)
	_release_plot_if_no_active_application(doc)


@frappe.whitelist()
def close_sales_order(sales_order_name):
	"""Close a plot Sales Order with no payments — releases plot, declines TCB CN, cancels application."""
	frappe.only_for("LandMS Sales Manager")

	doc = frappe.get_doc("Sales Order", sales_order_name)
	if not _is_landms_sales_order(doc):
		frappe.throw("This Sales Order is not a LandMS plot sale.")
	if doc.docstatus != 1:
		frappe.throw("Only submitted Sales Orders can be closed.")
	if doc.status == "Closed":
		frappe.throw("This Sales Order is already closed.")

	_block_close_if_paid(doc)

	# DECLINE FIRST — the TCB decline is the gate. If the control number cannot
	# be declined (e.g. TCB unreachable), abort BEFORE posting the forfeiture JE
	# or releasing the plot, so we never leave a half-closed order or a live
	# orphaned control number that could still take a payment. Nothing below
	# runs until the number is dead. An already-declined number returns ok=True,
	# so a retry (or the nightly re-run) passes straight through.
	control_number = cstr(doc.get("control_number") or "").strip()
	if control_number:
		result = decline_reference_for_sales_order(doc.name, control_number)
		if not result.get("ok"):
			frappe.throw(
				f"This order could not be closed yet because the bank could not be "
				f"reached to release its payment number. Nothing has been changed on "
				f"this order. Please try closing it again after a while. "
				f"(Ref {control_number}: {result.get('message')})"
			)

	_post_draft_contract_forfeiture_je(doc)
	_clear_application_sales_order_link(doc)
	_cancel_unpaid_plot_sales_invoice(doc)
	if doc.get("forfeiture_entry"):
		si_name = doc.get("plot_sales_invoice")
		if si_name:
			_post_credit_note_for_outstanding(si_name)
	_delete_draft_plot_contract(doc)

	_cancel_plot_application(doc)
	_release_plot_if_no_active_application(doc)

	# update_modified=True so the "Last edited by" footprint records WHO closed the
	# Sales Order (the user who clicked Close), and an explicit timeline comment makes
	# it a permanent audit trail that later edits can't overwrite.
	doc.db_set("status", "Closed", update_modified=True)
	doc.add_comment("Info", f"Sales Order closed by {frappe.session.user}.")

	# Mirror the termination alert — tell the user what was forfeited / refunded.
	close_msg = f"Sales Order {doc.name} closed. Plot {doc.get('plot') or ''} released."
	if doc.get("forfeiture_entry"):
		fje = frappe.get_doc("Journal Entry", doc.forfeiture_entry)
		forfeited = flt(fje.total_debit)
		refund = flt(fje.get("lms_refund_amount"))
		close_msg += f" TZS {forfeited:,.0f} forfeited (net of government share)" + (
			f"; TZS {refund:,.0f} refund due to customer." if refund > 0 else "."
		)
	frappe.msgprint(close_msg, indicator="orange", alert=True)


def _refuse_reopen_if_terminated(sales_order_name):
	"""A terminated contract is final (operator rule, 14 Oct 2026): the customer
	failed for good, so its order is never reopened — not by LandMS's Reopen,
	not by ERPNext's Status > Re-open. The order stays Closed (not cancelled)
	because its invoice, payments and forfeiture are the record."""
	if frappe.db.exists(
		"Plot Contract", {"sales_order": sales_order_name, "contract_status": "Terminated", "docstatus": 1}
	):
		frappe.throw(
			f"Sales Order {sales_order_name} cannot be reopened: its contract was terminated, and a "
			f"termination is final. A returning customer needs a new order."
		)


def before_change_sales_order(doc, method=None):
	"""Every db_set on a Sales Order runs this — ERPNext's Status > Re-open sets
	the status that way. Refuse lifting Closed from a terminated plot order.
	LandMS's own Reopen writes the status directly and checks the same rule."""
	if doc.docstatus != 1 or doc.get("status") == "Closed" or not doc.get("plot"):
		return
	if frappe.db.get_value("Sales Order", doc.name, "status") == "Closed":
		_refuse_reopen_if_terminated(doc.name)


@frappe.whitelist(methods=["POST"])  # it commits mid-way: never reachable by a plain link (GET)
def reopen_sales_order(sales_order_name: str):
	"""Reopen a Closed / forfeited plot Sales Order (productizes operator Scripts F + G).

	REVERSE-FIRST (mirrors running Script F, then Script G — the operator's proven order):

	  1. Reverse the forfeiture (Script F), atomically, with NO bank call inside: cancel the
	     credit note + forfeiture JE, KEEP the government-share JE (never reversed), restore the
	     deleted Plot Contract, re-link SO/SI/PA, restore the application to Paid with a fresh
	     7-day advance window, move the plot back to Pending Advance, set the order active.
	     Any assert failure rolls the WHOLE reversal back. Then COMMIT.
	  2. Re-register the control number at the bank (Script G) as a SEPARATE step, AFTER the
	     reversal has committed. This never rolls the reopen back: on success the number is
	     Registered; on a bank error it is left Failed and the operator retries via the button.

	Doing the DB reversal first (no irreversible bank call inside the locked block) is what
	kills the old bug class — no mid-reversal commit to release locks / break the snapshot, and
	a bank outage no longer blocks the reopen, it just defers the re-registration. The order is
	reactivated either way; a payment can only land once the number is live again.
	"""
	import json

	# Reopen reverses a forfeiture — restrict to System Manager (more privileged than Close).
	frappe.only_for("System Manager")

	# Serialize concurrent reopens of the SAME order (double-click / two tabs). Lock the
	# SO row FOR UPDATE so a second invocation waits here; by the time it proceeds the
	# first has committed and the order is no longer Closed, so it aborts cleanly at the
	# status guard below instead of both running the reversal and corrupting state.
	if not frappe.db.exists("Sales Order", sales_order_name):
		frappe.throw(f"Sales Order {sales_order_name} was not found.")
	# Serialize concurrent reopens with a MySQL NAMED lock (GET_LOCK), NOT a row
	# FOR UPDATE lock. The re-register step below commits mid-request (create_tcb_api_log
	# / mark_registered), and a COMMIT releases FOR UPDATE row locks — which would let a
	# second concurrent reopen slip through. A named lock survives commits; it is held for
	# the whole request and auto-released when the request's DB connection closes. A second
	# reopen blocks here, then finds the order no longer Closed and aborts at the status guard.
	_lock = f"reopen_so:{sales_order_name}"[:64]
	_got = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (_lock, 30))
	if not (_got and _got[0] and _got[0][0] == 1):
		frappe.throw(
			f"Another reopen of {sales_order_name} is already in progress — please try again in a moment."
		)

	so = frappe.get_doc("Sales Order", sales_order_name)
	if not _is_landms_sales_order(so):
		frappe.throw("This Sales Order is not a LandMS plot sale.")
	_refuse_reopen_if_terminated(so.name)
	# "Close for good at Malipopay" is final: its Malipopay bill is expired and a
	# returning customer is a new sale.
	from landms.malipopay import is_closed_for_good

	if is_closed_for_good(so.name):
		frappe.throw(
			f"Sales Order {so.name} was closed for good at Malipopay and can no longer be reopened. "
			"A returning customer needs a new order."
		)

	SO = so.name
	PLOT = so.plot
	PA = so.plot_application
	PLOT_SI = so.plot_sales_invoice
	JE = so.forfeiture_entry

	# Row-lock the Plot Master FOR UPDATE so a concurrent re-sale of the SAME plot via a
	# DIFFERENT Sales Order (a colleague submitting a new application / contract / order)
	# serializes against this reopen. The named lock above only guards two reopens of the
	# same ORDER; it does not cover a re-sale on another order. Holding the plot row means the
	# plot-free eligibility guards below are read under the lock, closing the TOCTOU window
	# between "checked the plot is free" and "re-reserved the plot". The lock is held until
	# the reversal commits (line ~470).
	if PLOT:
		frappe.db.sql("select name from `tabPlot Master` where name=%s for update", (PLOT,))

	# ---- eligibility guards (Script F) — throw a clear reason if not reopenable ----
	# BUSINESS RULE: only a CLOSED order can be reopened — a Closed order had money (a
	# payment that was forfeited). A CANCELLED order is never reopenable: cancellation only
	# happens for UNPAID orders (no money), so there is nothing to reactivate. (The button
	# is also hidden on cancelled orders — they are docstatus 2, not 1.)
	if so.status != "Closed":
		frappe.throw(
			f"Sales Order {SO} cannot be reopened (current status: {so.status}). Only a "
			f"CLOSED, forfeited order — one that had a payment — can be reopened. A cancelled "
			f"order had no payment and cannot be reactivated."
		)
	if not JE:
		frappe.throw(f"Sales Order {SO} has no forfeiture entry to reverse (no money was forfeited).")
	plot_status = frappe.db.get_value("Plot Master", PLOT, "status")
	active_app = frappe.db.exists(
		"Plot Application",
		{"plot": PLOT, "docstatus": 1, "status": ["in", ["Submitted", "Paid", "Converted"]]},
	)
	active_ctr = frappe.db.exists("Plot Contract", {"plot": PLOT, "docstatus": 1})
	if not (plot_status == "Available" and not active_app and not active_ctr):
		frappe.throw(
			f"Cannot reopen {SO} — the plot {PLOT} is not free (status: {plot_status}"
			+ (", has an active application" if active_app else "")
			+ (", has an active contract" if active_ctr else "")
			+ "). It may have been re-sold."
		)
	# Also reject an IN-PROGRESS (Draft) re-sale on the plot. The plot only flips to a
	# reserved status on submit, so a colleague's Draft application / contract / Sales
	# Order leaves it "Available" and the docstatus-1 checks above miss it. The old
	# application is docstatus 2 and the old contract is deleted, so ANY Draft claim on
	# this plot (other than this SO) is a NEW re-sale we must not clobber.
	draft_app = frappe.db.exists("Plot Application", {"plot": PLOT, "docstatus": 0})
	draft_ctr = frappe.db.exists("Plot Contract", {"plot": PLOT, "docstatus": 0})
	other_draft_so = frappe.db.exists("Sales Order", {"plot": PLOT, "docstatus": 0, "name": ["!=", SO]})
	if draft_app or draft_ctr or other_draft_so:
		frappe.throw(
			f"Cannot reopen {SO} — the plot {PLOT} has an in-progress (Draft) re-sale "
			f"(application/contract/order) that would be overridden. Resolve or cancel that first."
		)
	je_date = frappe.db.get_value("Journal Entry", JE, "posting_date")
	froz = frappe.db.get_single_value("Accounts Settings", "acc_frozen_upto")
	if froz and frappe.utils.getdate(froz) >= frappe.utils.getdate(je_date):
		frappe.throw(
			f"Cannot reopen {SO} — the forfeiture ({je_date}) is in a frozen accounting period (up to {froz})."
		)
	if not (
		frappe.db.get_value("Plot Application", PA, "docstatus") == 2
		and frappe.db.get_value("Plot Application", PA, "status") in ("Cancelled", "Expired")
	):
		frappe.throw(f"Cannot reopen {SO} — its application {PA} is not in a cancelled/expired state.")

	# Partial-forfeiture safety: a partial close records a customer refund
	# (lms_refund_amount) that stays parked in Customer Advance and the accountant may
	# already have PAID it out. Reopen re-parks the forfeited amount as if the full advance
	# is still on deposit, so re-crediting an already-paid refund would double-count. If ANY
	# payout to this customer exists since the forfeiture, block and require manual accounting
	# review. (No-op on a 100%-forfeiture config where lms_refund_amount is always 0.)
	#
	# A refund can be paid two ways, so we check BOTH: a Pay-type Payment Entry, OR a manual
	# Journal Entry that touches this customer as a party (Dr Customer Advance / Cr Bank). The
	# JE check is deliberately broad — any submitted JE since the forfeiture with this customer
	# on a line — because we would rather block a legitimate reopen and have the accountant
	# confirm than silently double-credit real money.
	refund_due = flt(frappe.db.get_value("Journal Entry", JE, "lms_refund_amount") or 0)
	if refund_due > 0:
		payout = frappe.db.get_value(
			"Payment Entry",
			{
				"party_type": "Customer",
				"party": so.customer,
				"payment_type": "Pay",
				"docstatus": 1,
				"posting_date": [">=", je_date],
			},
			"name",
		)
		if not payout:
			# A JE refund: a submitted Journal Entry since the forfeiture with this customer as
			# a party on any line (Dr Customer Advance / Cr Bank). The forfeiture and govt-share
			# JEs post to the Customer-Advance account with NO party dimension, so they never
			# match this party filter — only a genuine party-tagged refund JE does. The
			# forfeiture JE is excluded explicitly as a belt-and-braces guard.
			je_rows = frappe.db.sql(
				"""select distinct jea.parent
				   from `tabJournal Entry Account` jea
				   join `tabJournal Entry` je on je.name = jea.parent
				   where jea.party_type = 'Customer' and jea.party = %s
				     and je.docstatus = 1 and je.posting_date >= %s
				     and jea.parent != %s""",
				(so.customer, je_date, JE),
			)
			payout = je_rows[0][0] if je_rows else None
		if payout:
			frappe.throw(
				f"Cannot reopen {SO} — a customer refund of TZS {refund_due:,.0f} was recorded on the "
				f"forfeiture, and a payout / entry to {so.customer} exists since then ({payout}). Reopening "
				f"would double-credit the customer. Reverse the payout / handle this manually in accounting first."
			)

	# locate the deleted Plot Contract to restore
	DC = DD = dc_data = None
	for d in frappe.get_all(
		"Deleted Document",
		filters={"deleted_doctype": "Plot Contract", "restored": 0},
		fields=["name", "deleted_name", "data"],
	):
		o = json.loads(d["data"])
		if o.get("sales_order") == SO:
			DD, DC, dc_data = d["name"], d["deleted_name"], o
			break
	if not DC:
		frappe.throw(f"Cannot reopen {SO} — the original Plot Contract could not be found to restore.")

	# cash / outstanding / government-share figures (Script F)
	cash = (
		flt(
			frappe.db.sql(
				"select coalesce(sum(per.allocated_amount),0) from `tabPayment Entry Reference` per join `tabPayment Entry` pe on pe.name=per.parent where per.reference_doctype='Sales Invoice' and per.reference_name=%s and pe.docstatus=1 and pe.payment_type='Receive'",
				(PLOT_SI,),
			)[0][0]
		)
		if PLOT_SI
		else 0
	)
	grand = flt(frappe.db.get_value("Sales Invoice", PLOT_SI, "grand_total")) if PLOT_SI else 0
	unpaid = grand - cash
	pes = (
		[
			r[0]
			for r in frappe.db.sql(
				"select distinct per.parent from `tabPayment Entry Reference` per join `tabPayment Entry` pe on pe.name=per.parent where per.reference_doctype='Sales Invoice' and per.reference_name=%s and pe.docstatus=1",
				(PLOT_SI,),
			)
		]
		if PLOT_SI
		else []
	)
	# Query govt-share JEs only when there ARE payment PEs. ["in", [""]] would match every JE
	# with a NULL/empty lms_payment_entry (Frappe wraps it in coalesce), which sweeps in the
	# forfeiture JE itself — so guard on pes being non-empty.
	govt = (
		frappe.get_all(
			"Journal Entry", filters={"lms_payment_entry": ["in", pes], "docstatus": 1}, pluck="name"
		)
		if pes
		else []
	)
	G = len(govt)
	CN_INV = (
		frappe.db.get_value(
			"Sales Invoice", {"return_against": PLOT_SI, "is_return": 1, "docstatus": 1}, "name"
		)
		if PLOT_SI
		else None
	)

	control_number = cstr(so.get("control_number") or "").strip()

	# ---- REVERSE FIRST (Script F) — atomic; the SO row is LOCKED and there is NO bank call
	# in this block, so a concurrent re-sale can't slip in and any assert failure rolls the
	# whole reversal back. The bank re-registration is a SEPARATE step AFTER this commits
	# (mirrors Script G after Script F) — no irreversible bank call sits inside the reversal. ----
	if CN_INV and frappe.db.get_value("Sales Invoice", CN_INV, "docstatus") == 1:
		frappe.get_doc("Sales Invoice", CN_INV).cancel()
	frappe.db.set_value("Sales Order", SO, "forfeiture_entry", None, update_modified=False)
	if frappe.db.get_value("Journal Entry", JE, "docstatus") == 1:
		frappe.get_doc("Journal Entry", JE).cancel()
	for gj in govt:  # government-share JE is KEPT (never reversed) — verify it is untouched
		if frappe.db.get_value("Journal Entry", gj, "docstatus") != 1:
			frappe.throw(f"{SO}: government-share JE {gj} changed unexpectedly — aborting reopen.")
	if not frappe.db.exists("Plot Contract", DC):
		nd = frappe.get_doc(dc_data)
		nd.flags.ignore_permissions = True
		nd.insert(set_name=DC)
		frappe.db.set_value("Deleted Document", DD, {"restored": 1, "new_name": DC}, update_modified=False)
		# The contract's validate() recomputes total_paid from its own payment_schedule /
		# SI-outstanding, and the exact value it lands on depends on recompute ordering at
		# insert time (calculate_payment_summary vs sync_payment_status). Pin total_paid back
		# to the authoritative figure — cash actually received (sum of Receive PE allocations
		# to the plot invoice) — with a direct write that bypasses the recompute, so the
		# restored contract reflects reality and the total_paid == cash assert below is a true
		# check of the restore, not of recompute timing.
		frappe.db.set_value("Plot Contract", DC, "total_paid", cash, update_modified=False)
	frappe.db.set_value("Sales Order", SO, "plot_contract", DC, update_modified=False)
	if PLOT_SI:
		frappe.db.set_value("Sales Invoice", PLOT_SI, "plot_contract", DC, update_modified=False)
		frappe.db.set_value("Sales Order", SO, "plot_sales_invoice", PLOT_SI, update_modified=False)
	frappe.db.set_value("Plot Application", PA, "sales_order", SO, update_modified=False)
	frappe.db.set_value("Sales Order", SO, "status", "To Deliver and Bill", update_modified=False)
	frappe.db.set_value(
		"Plot Application", PA, {"docstatus": 1, "status": "Paid", "sales_order": SO}, update_modified=False
	)
	set_plot_status(
		PLOT,
		"Pending Advance",
		f"sales order {link('Sales Order', SO)} was reopened",
		only_from=("Available",),
		update_modified=False,
	)

	# fresh 7-day advance window (payment_date back-set so expiry lands exactly 7 days out).
	# VALID must mirror the expiry task's window for THIS plot: a no_advance_deadline
	# plot uses its full payment_completion_days, others the global setting — so the
	# back-set below always lands the effective expiry exactly 7 days out either way.
	_plot_rule = (
		frappe.db.get_value(
			"Plot Master", PLOT, ["no_advance_deadline", "payment_completion_days"], as_dict=True
		)
		or frappe._dict()
	)
	if cint(_plot_rule.no_advance_deadline) and cint(_plot_rule.payment_completion_days) > 0:
		VALID = cint(_plot_rule.payment_completion_days)
	else:
		VALID = int(frappe.db.get_single_value("LandMS Settings", "application_fee_validity_days") or 7)
	new_pd = frappe.utils.add_days(frappe.utils.today(), -(VALID - 7))
	new_exp = frappe.utils.add_days(frappe.utils.today(), 7)
	orig_pd = frappe.db.get_value("Plot Application", PA, "payment_date")
	frappe.db.set_value(
		"Plot Application", PA, {"payment_date": new_pd, "expiry_date": new_exp}, update_modified=False
	)
	frappe.get_doc("Plot Application", PA).add_comment(
		"Info",
		f"Reactivated from forfeiture by {frappe.session.user}; original payment_date was {orig_pd}; fresh 7-day advance window (expiry {new_exp}).",
	)
	so.add_comment("Info", f"Sales Order reopened from forfeiture by {frappe.session.user}.")

	# ---- per-record asserts (Script F) — any failure aborts the reopen ----
	tp = flt(frappe.db.get_value("Plot Contract", DC, "total_paid"))
	if abs(tp - cash) >= 1:
		frappe.throw(f"{SO}: restored contract total_paid {tp} != cash {cash} — aborted.")
	if (
		PLOT_SI
		and abs(flt(frappe.db.get_value("Sales Invoice", PLOT_SI, "outstanding_amount")) - unpaid) >= 1
	):
		frappe.throw(f"{SO}: invoice outstanding does not match unpaid balance — aborted.")
	if CN_INV and frappe.db.get_value("Sales Invoice", CN_INV, "docstatus") != 2:
		frappe.throw(f"{SO}: credit note was not cancelled — aborted.")
	if frappe.db.get_value("Journal Entry", JE, "docstatus") != 2:
		frappe.throw(f"{SO}: forfeiture JE was not cancelled — aborted.")
	current_govt = (
		len(
			frappe.get_all(
				"Journal Entry", filters={"lms_payment_entry": ["in", pes], "docstatus": 1}, pluck="name"
			)
		)
		if pes
		else 0
	)
	if current_govt != G:
		frappe.throw(f"{SO}: government-share JE count changed — aborted.")
	if frappe.db.get_value("Sales Order", SO, "status") == "Closed":
		frappe.throw(f"{SO}: still Closed after reopen — aborted.")

	# The DB reversal is complete and consistent — COMMIT it so it is durable. From here on the
	# order is reactivated and the plot re-reserved; the bank re-registration below can NEVER
	# roll it back (mirrors committing Script F before running Script G).
	frappe.db.commit()

	# ---- THEN RE-REGISTER with the bank (Script G, the separate second step) ----
	registered, reg_msg = True, ""
	if control_number:
		reg = _reregister_after_reopen(SO, control_number)
		registered, reg_msg = reg.get("registered", False), reg.get("message") or ""
		# The close expired the Malipopay bill; bring one back (never blocks the reopen).
		from landms.malipopay import reopen_collection_for_sales_order

		reopen_collection_for_sales_order(SO)

	if registered:
		frappe.msgprint(
			f"Sales Order {SO} reopened. Plot {PLOT} reserved again; fresh 7-day advance window "
			f"(expiry {new_exp}). Payment number re-registered.",
			indicator="green",
			alert=True,
		)
	else:
		frappe.msgprint(
			f"Sales Order {SO} reopened and the plot is reserved again (fresh 7-day window, expiry "
			f"{new_exp}) — but the payment number could NOT be re-registered yet ({reg_msg}). Use "
			f"Retry Registration on the control number once the bank is reachable.",
			indicator="orange",
			alert=True,
		)
	return {"ok": True, "sales_order": SO, "plot": PLOT, "expiry": str(new_exp), "registered": registered}


def _reregister_after_reopen(sales_order_name, control_number):
	"""Re-register a reopened order's control number with the bank — the SEPARATE second step,
	run AFTER the reopen's DB reversal has already COMMITTED (mirrors Script G after Script F).

	This NEVER rolls the reopen back — the order is already reactivated. It marks the number
	'Reopened', registers it, and reports the outcome:
	  - success            -> Registered   (returns registered=True)
	  - bank error          -> Failed       (returns registered=False — retry later via the button)
	  - already Registered  -> nothing to do

	Only re-registers a number that is genuinely DEAD at the bank (its most recent Live event
	was a successful decline); a still-live/paid number is left untouched.
	"""
	from landms.tcb import _get_registry, register_reference_for_sales_order

	registry = _get_registry(control_number)
	if not registry:
		return {"registered": False, "message": f"no registry row for {control_number}"}
	if registry.status == "Registered":
		return {"registered": True, "message": "already registered"}
	if registry.status == "Paid":
		# Require the MOST RECENT Live register/decline event to be a successful Decline, so
		# we never re-register a number that is still live/paid at the bank.
		last = frappe.get_all(
			"TCB API Log",
			filters={
				"external_reference": control_number,
				"processing_mode": "Live",
				"status": "Success",
				"event_type": ["in", ["Reference Create", "Reference Decline"]],
			},
			fields=["event_type"],
			order_by="creation desc",
			limit=1,
		)
		if not (last and last[0].event_type == "Reference Decline"):
			return {
				"registered": False,
				"message": f"{control_number} is Paid and may still be live at the bank — left as-is; review manually",
			}

	# Mark the number 'Reopened' so its status reflects the reactivation.
	if registry.status in ("Paid", "Declined", "Expired"):
		frappe.get_doc("TCB Control Number", control_number).mark_reopened()

	# Register with the bank. On success -> Registered; on a bank error -> Failed (which is
	# retryable via the Retry Registration button). Key success off the CALL's ok flag, NOT the
	# persisted status — in Off / Log Only mode register returns ok without setting 'Registered'.
	result = register_reference_for_sales_order(sales_order_name, control_number)
	if result.get("ok"):
		return {"registered": True, "message": "re-registered"}
	return {"registered": False, "message": result.get("message") or "bank re-registration failed"}


def ensure_plot_sales_invoice_for_sales_order(
	sales_order_name: str,
	*,
	posting_date: str | None = None,
) -> str:
	"""Create the single plot SI on first advance, or return the existing one.

	This is called from:
	  - TCB inbound payment application
	  - manual Payment Entry submit against the Sales Order
	"""
	if not sales_order_name or not frappe.db.exists("Sales Order", sales_order_name):
		frappe.throw(f"Sales Order {sales_order_name} was not found.")

	doc = frappe.get_doc("Sales Order", sales_order_name)
	contract_name = _ensure_draft_plot_contract(doc)
	invoice_name = _ensure_plot_sales_invoice(doc, contract_name, posting_date=posting_date)

	if invoice_name and doc.get("plot_sales_invoice") != invoice_name:
		doc.db_set("plot_sales_invoice", invoice_name, update_modified=False)

	return invoice_name


def build_sales_order_item_row(plot, warehouse, delivery_date):
	item_code = get_plot_item_code(plot.plot_type)

	item = frappe.db.get_value(
		"Item",
		item_code,
		["name", "item_name", "stock_uom"],
		as_dict=True,
	)
	if not item:
		frappe.throw(f"Item {item_code} was not found.")
	if not item.stock_uom:
		frappe.throw(f"Item {item_code} is missing Stock UOM.")

	return {
		"item_code": item.name,
		"item_name": item.item_name,
		"uom": item.stock_uom,
		"stock_uom": item.stock_uom,
		"conversion_factor": 1,
		"qty": 1,
		"rate": flt(plot.selling_price),
		"warehouse": warehouse,
		"delivery_date": delivery_date,
	}


def build_payment_schedule_rows(total_amount, booking_fee_percent, transaction_date, payment_deadline):
	total_amount = flt(total_amount)
	booking_fee_percent = max(0.0, min(100.0, flt(booking_fee_percent)))
	transaction_date = transaction_date or today()
	payment_deadline = payment_deadline or transaction_date

	if total_amount <= 0:
		return []

	# If no booking fee or both dates are the same, single row
	if booking_fee_percent <= 0 or str(transaction_date) == str(payment_deadline):
		return [
			{
				"payment_term": "Advance",
				"description": "Full Plot Payment",
				"due_date": payment_deadline,
				"invoice_portion": 100.0,
				"payment_amount": total_amount,
			}
		]

	booking_amount = flt(total_amount * booking_fee_percent / 100)
	balance_amount = flt(total_amount - booking_amount)
	rows = [
		{
			"payment_term": "Advance",
			"description": "Advance",
			"due_date": transaction_date,
			"invoice_portion": booking_fee_percent,
			"payment_amount": booking_amount,
		}
	]

	if balance_amount > 0:
		rows.append(
			{
				"payment_term": "Balance",
				"description": "Balance",
				"due_date": payment_deadline,
				"invoice_portion": flt(100 - booking_fee_percent),
				"payment_amount": balance_amount,
			}
		)

	return rows


def _is_landms_sales_order(doc) -> bool:
	return bool(doc.get("plot_application") or doc.get("plot"))


def _get_application(doc, required=True):
	if not doc.get("plot_application"):
		if required:
			frappe.throw("Plot Application is required for LandMS plot Sales Orders.")
		return None

	if not frappe.db.exists("Plot Application", doc.plot_application):
		if required:
			frappe.throw(f"Plot Application {doc.plot_application} was not found.")
		return None

	app = frappe.get_doc("Plot Application", doc.plot_application)
	if required and (app.docstatus != 1 or app.status not in ("Paid", "Converted")):
		frappe.throw(
			f"Plot Application {app.name} must be submitted and fee-paid before creating a Sales Order."
		)
	return app


def _get_plot(doc):
	if not doc.get("plot"):
		frappe.throw("Plot is required for LandMS Sales Orders.")
	if not frappe.db.exists("Plot Master", doc.plot):
		frappe.throw(f"Plot {doc.plot} was not found.")
	return frappe.get_doc("Plot Master", doc.plot)


def _validate_application_window(application):
	if application.expiry_date and getdate(application.expiry_date) < getdate(today()):
		frappe.throw(f"Plot Application {application.name} has expired. The reservation window has ended.")


def _validate_plot_state(plot):
	if plot.status not in ("Pending Advance", "Available"):
		frappe.throw(
			f"Plot {plot.name} is not ready for Sales Order creation (current status: {plot.status})."
		)


def _ensure_single_sales_order_for_application(doc, application):
	existing = frappe.db.get_value(
		"Sales Order",
		{
			"plot_application": application.name,
			"name": ("!=", doc.name),
			"docstatus": ("!=", 2),
		},
		"name",
	)
	if existing:
		frappe.throw(f"Plot Application {application.name} is already linked to Sales Order {existing}.")


def _ensure_items(doc, plot, settings):
	delivery_date = add_days(doc.transaction_date or today(), cint(doc.payment_completion_days or 0))
	row_values = build_sales_order_item_row(plot, settings.plot_inventory_warehouse, delivery_date)
	row_values["cost_center"] = settings.cost_center
	row_values["land_acquisition"] = doc.land_acquisition

	if len(doc.items or []) == 1 and doc.items[0].get("item_code") == row_values["item_code"]:
		row = doc.items[0]
		for key, value in row_values.items():
			row.set(key, value)
		return

	doc.set("items", [row_values])


def _ensure_payment_schedule(doc, plot):
	schedule_rows = build_payment_schedule_rows(
		total_amount=flt(plot.selling_price),
		booking_fee_percent=flt(doc.booking_fee_percent),
		transaction_date=doc.transaction_date,
		payment_deadline=doc.payment_deadline,
	)

	doc.set("payment_schedule", [])
	for row in schedule_rows:
		doc.append("payment_schedule", row)


def _ensure_draft_plot_contract(doc):
	if doc.get("plot_contract") and frappe.db.exists("Plot Contract", doc.plot_contract):
		return _sync_existing_draft_plot_contract(doc.plot_contract, doc)

	existing = frappe.db.get_value(
		"Plot Contract",
		{"sales_order": doc.name, "docstatus": ("!=", 2)},
		"name",
	)
	if existing:
		return _sync_existing_draft_plot_contract(existing, doc)

	contract = frappe.get_doc(
		{
			"doctype": "Plot Contract",
			"customer": doc.customer,
			"plot": doc.plot,
			"plot_application": doc.get("plot_application") or "",
			"contract_date": doc.transaction_date or today(),
			"payment_completion_days": cint(doc.payment_completion_days or 0),
			"payment_deadline": doc.payment_deadline,
			"apply_auto_cancellation": cint(doc.get("apply_auto_cancellation", 1)),
			"sales_order": doc.name,
			"control_number": doc.get("control_number") or "",
			"booking_fee_percent": flt(doc.booking_fee_percent),
			"government_share_percent": flt(doc.government_share_percent),
			"notes": doc.terms or "",
		}
	)
	_sync_contract_schedule(contract, doc)
	contract.flags.from_sales_order = True
	contract.insert(ignore_permissions=True)
	return contract.name


def _sync_existing_draft_plot_contract(contract_name: str, source_doc) -> str:
	"""Refresh a draft Plot Contract from the Sales Order without failing on harmless races."""
	for attempt in range(2):
		contract = frappe.get_doc("Plot Contract", contract_name)
		if contract.docstatus != 0:
			return contract.name

		if _draft_contract_matches_sales_order(contract, source_doc):
			return contract.name

		try:
			_sync_contract_schedule(contract, source_doc)
			contract.flags.from_sales_order = True
			contract.save(ignore_permissions=True)
			return contract.name
		except frappe.TimestampMismatchError:
			if attempt:
				raise

	return contract_name


def _draft_contract_matches_sales_order(contract, source_doc) -> bool:
	if cstr(contract.control_number or "") != cstr(source_doc.get("control_number") or ""):
		return False
	if cstr(contract.plot_application or "") != cstr(source_doc.get("plot_application") or ""):
		return False
	if cstr(contract.payment_deadline or "") != cstr(source_doc.get("payment_deadline") or ""):
		return False
	if cint(contract.apply_auto_cancellation or 0) != cint(source_doc.get("apply_auto_cancellation", 1)):
		return False

	expected_rows = _build_contract_schedule_rows(source_doc)
	actual_rows = sorted(
		contract.get("payment_schedule") or [],
		key=lambda row: cint(getattr(row, "installment_number", 0) or 0),
	)
	if len(actual_rows) != len(expected_rows):
		return False

	for actual, expected in zip(actual_rows, expected_rows, strict=False):
		if cint(getattr(actual, "installment_number", 0) or 0) != cint(expected["installment_number"]):
			return False
		if cstr(getattr(actual, "description", "") or "") != cstr(expected["description"] or ""):
			return False
		if cstr(getattr(actual, "due_date", "") or "") != cstr(expected["due_date"] or ""):
			return False
		if flt(getattr(actual, "expected_amount", 0) or 0) != flt(expected["expected_amount"]):
			return False
		if flt(getattr(actual, "paid_amount", 0) or 0) != flt(expected["paid_amount"]):
			return False
		if flt(getattr(actual, "outstanding_amount", 0) or 0) != flt(expected["outstanding_amount"]):
			return False
		if cstr(getattr(actual, "paid_date", "") or "") != cstr(expected["paid_date"] or ""):
			return False
		if cstr(getattr(actual, "sales_invoice", "") or "") != cstr(expected["sales_invoice"] or ""):
			return False

		if cstr(getattr(actual, "status", "") or "") != cstr(expected["status"] or ""):
			return False

	return True


def _build_contract_schedule_rows(source_doc) -> list[dict]:
	rows = []
	for idx, row in enumerate(source_doc.get("payment_schedule") or [], start=1):
		expected_amount = flt(getattr(row, "payment_amount", 0))
		rows.append(
			{
				"installment_number": idx,
				"description": getattr(row, "description", "") or f"Installment {idx}",
				"due_date": row.due_date,
				"expected_amount": expected_amount,
				"paid_amount": 0,
				"outstanding_amount": expected_amount,
				"paid_date": None,
				"sales_invoice": "",
				"status": "Pending",
			}
		)
	return rows


def _sync_contract_schedule(contract, source_doc):
	contract.control_number = source_doc.get("control_number") or ""
	contract.plot_application = source_doc.get("plot_application") or ""
	contract.payment_deadline = source_doc.get("payment_deadline")
	contract.apply_auto_cancellation = cint(source_doc.get("apply_auto_cancellation", 1))
	contract.set("payment_schedule", [])

	for row in _build_contract_schedule_rows(source_doc):
		contract.append("payment_schedule", row)


def _ensure_plot_sales_invoice(doc, contract_name, *, posting_date: str | None = None):
	existing_invoice_name = doc.get("plot_sales_invoice")
	if existing_invoice_name and frappe.db.exists("Sales Invoice", existing_invoice_name):
		_link_plot_invoice_to_sales_order(doc, existing_invoice_name)
		_link_plot_invoice_to_contract(contract_name, existing_invoice_name)
		return existing_invoice_name

	# Find an existing plot SI for THIS customer+plot whose SO is still active.
	# Audit-trail SIs from cancelled or closed SOs must not be reused: they belong
	# to a different sale and would cross-contaminate payment allocation.
	# Closed SOs keep docstatus=1 so we must also exclude status='Closed'.
	candidates = frappe.db.sql(
		"""
		SELECT si.name
		FROM `tabSales Invoice` si
		LEFT JOIN `tabSales Invoice Item` sii ON sii.parent = si.name
		LEFT JOIN `tabSales Order` so ON so.name = sii.sales_order
		WHERE si.plot = %s
		  AND si.customer = %s
		  AND si.is_plot_sale_invoice = 1
		  AND si.is_return = 0
		  AND si.docstatus = 1
		  AND (so.name IS NULL OR (so.docstatus = 1 AND so.status NOT IN ('Closed', 'Cancelled')))
		LIMIT 1
		""",
		(doc.plot, doc.customer),
	)
	existing = candidates[0][0] if candidates else None
	if existing:
		_link_plot_invoice_to_sales_order(doc, existing)
		_link_plot_invoice_to_contract(contract_name, existing)
		return existing

	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

	posting_date = posting_date or today()

	# Elevate to Administrator around the ENTIRE invoice creation — build,
	# account/item writes, and insert/submit. ERPNext 15.116+ added
	# account_perm_check() inside get_party_account(), so building the invoice
	# (make_sales_invoice -> set_missing_values -> get_party_account) reads the
	# customer's receivable account and raises PermissionError for a Guest caller
	# (the public TCB IPN runs as Guest). Previously only insert/submit were
	# elevated, so a new order's first live payment failed at the build step.
	caller = become_administrator()
	try:
		invoice = make_sales_invoice(doc.name, ignore_permissions=True)
		invoice.posting_date = posting_date
		invoice.due_date = doc.payment_deadline or add_days(
			doc.transaction_date or posting_date, cint(doc.payment_completion_days or 0)
		)
		invoice.ignore_default_payment_terms_template = 1
		invoice.allocate_advances_automatically = 0
		invoice.plot = doc.plot
		invoice.land_acquisition = doc.land_acquisition
		invoice.plot_contract = contract_name or ""
		invoice.is_plot_sale_invoice = 1
		invoice.update_stock = 0
		invoice.enabled_auto_create_delivery_notes = 0
		invoice.is_not_vfd_invoice = 1
		invoice.remarks = f"Plot sale invoice for {doc.plot} via Sales Order {doc.name}"

		# Deferred revenue: all customer payments sit in Customer Advances (liability)
		# until the contract is fully paid. Revenue is recognised only at completion
		# via the JE in plot_contract._post_completion_entries — never on the SI itself.
		settings = frappe.get_single("LandMS Settings")
		customer_advance_account = settings.customer_advance_account
		cost_center = settings.cost_center
		for item in invoice.get("items") or []:
			item.income_account = customer_advance_account
			item.cost_center = cost_center
			item.enable_deferred_revenue = 0
			item.deferred_revenue_account = ""
			item.service_start_date = None
			item.service_end_date = None

		# Copy payment schedule from SO
		invoice.set("payment_schedule", [])
		for row in doc.get("payment_schedule") or []:
			invoice.append(
				"payment_schedule",
				{
					"payment_term": row.get("payment_term") or row.description,
					"description": row.description,
					"due_date": row.due_date,
					"invoice_portion": flt(row.invoice_portion),
					"payment_amount": flt(row.payment_amount),
				},
			)

		invoice.insert(ignore_permissions=True)
		invoice.submit()
	finally:
		restore_caller(caller)

	_link_plot_invoice_to_sales_order(doc, invoice.name)
	_link_plot_invoice_to_contract(contract_name, invoice.name)
	return invoice.name


def _link_plot_invoice_to_sales_order(doc, invoice_name):
	if not invoice_name or not frappe.db.exists("Sales Invoice", invoice_name):
		return

	if doc.get("plot_sales_invoice") != invoice_name:
		frappe.db.set_value(
			"Sales Order", doc.name, "plot_sales_invoice", invoice_name, update_modified=False
		)

	if not doc.items:
		return

	so_item_name = doc.items[0].name
	if not so_item_name:
		return

	invoice_items = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": invoice_name},
		fields=["name", "sales_order", "so_detail"],
	)
	for item in invoice_items:
		updates = {}
		if item.sales_order != doc.name:
			updates["sales_order"] = doc.name
		if not item.so_detail:
			updates["so_detail"] = so_item_name
		if updates:
			frappe.db.set_value("Sales Invoice Item", item.name, updates, update_modified=False)


def _link_plot_invoice_to_contract(contract_name, invoice_name):
	if not contract_name or not frappe.db.exists("Plot Contract", contract_name):
		return
	sales_order_name = frappe.db.get_value("Plot Contract", contract_name, "sales_order")
	control_number = ""
	plot_application = ""
	if sales_order_name and frappe.db.exists("Sales Order", sales_order_name):
		so_values = frappe.db.get_value(
			"Sales Order",
			sales_order_name,
			["control_number", "plot_application"],
			as_dict=True,
		)
		control_number = so_values.control_number or ""
		plot_application = so_values.plot_application or ""
	updates = {
		"booking_fee_invoice": invoice_name,
		"control_number": control_number,
		"plot_application": plot_application,
	}
	frappe.db.set_value("Plot Contract", contract_name, updates, update_modified=False)


def _ensure_control_number(doc) -> str:
	current = cstr(doc.get("control_number") or "").strip()
	if current and _reusable_number(current, doc.name):
		return current

	# One order, one number. A submit the bank refused (TCB down) has already
	# given this order a number — the TCB API log commits mid-transaction, so it
	# stays saved — but the form that pressed Submit never received it, and a
	# Save from that form blanks it again. Reuse it rather than mint a second
	# number the customer could be told and pay on (SO-05458, Oct 2026).
	new_cn = _number_already_held(doc) or generate_control_number(doc.name)
	doc.db_set("control_number", new_cn, update_modified=False)
	doc.control_number = new_cn
	return new_cn


def _number_already_held(doc) -> str:
	"""The payment number an earlier, failed submit of this order left behind:
	the one saved on the order, else the newest live registry row for it."""
	if doc.is_new():
		return ""
	stored = cstr(frappe.db.get_value("Sales Order", doc.name, "control_number") or "").strip()
	if stored and _reusable_number(stored, doc.name):
		return stored
	for name in frappe.get_all(
		"TCB Control Number",
		filters={"sales_order": doc.name, "status": ("in", ("Generated", "Failed", "Registered"))},
		order_by="creation desc",
		pluck="name",
	):
		if is_valid_control_number(name, sales_order_name=doc.name):
			return name
	return ""


# A number in one of these states is dead at TCB (or has already taken money):
# it is never handed to an order being submitted.
_DEAD_NUMBER_STATES = ("Declined", "Expired", "Paid")


def _reusable_number(number, sales_order_name) -> bool:
	"""A number this order may submit with: it fits the project's pattern, is
	not dead at TCB, and is not another order's. An amended or copied order can
	carry its original's number — switched off when the original was cancelled
	— and registering that again would leave the order stuck in Draft."""
	if not is_valid_control_number(number, sales_order_name=sales_order_name):
		return False
	row = frappe.db.get_value("TCB Control Number", number, ["status", "sales_order"], as_dict=True)
	if not row:
		return True
	return row.status not in _DEAD_NUMBER_STATES and (row.sales_order or sales_order_name) == sales_order_name


def _block_manual_control_number(doc):
	value = cstr(doc.get("control_number") or "").strip()
	if not value:
		return

	if not doc.is_new() and frappe.db.has_column("Sales Order", "control_number"):
		stored = frappe.db.get_value("Sales Order", doc.name, "control_number")
		if value == cstr(stored or "").strip():
			return

	if not is_valid_control_number(value, sales_order_name=doc.name):
		frappe.throw(
			"TCB Control Number cannot be set manually. It is generated by the "
			"system on Sales Order submit. Clear the field and re-save."
		)


def _create_registry_row(doc, control_number: str, related_control_number: str = ""):
	create_or_get_registry(
		control_number=control_number,
		sales_order=doc.name,
		customer=doc.customer,
		amount=flt(doc.grand_total),
		related_control_number=related_control_number,
	)


def _ensure_related_control_number(doc):
	if not cint(doc.get("include_related_ref")):
		return
	existing = cstr(doc.get("related_control_number") or "").strip()
	if existing and is_valid_control_number(existing, related=True, sales_order_name=doc.name):
		return
	# same as _ensure_control_number: reuse the related number a refused submit left behind
	primary_cn = cstr(doc.get("control_number") or "").strip()
	held = ""
	if not doc.is_new():
		held = frappe.db.get_value("Sales Order", doc.name, "related_control_number") or ""
		if not held and primary_cn:
			held = frappe.db.get_value("TCB Control Number", primary_cn, "related_control_number") or ""
	held = cstr(held).strip()
	if held and is_valid_control_number(held, related=True, sales_order_name=doc.name):
		doc.db_set("related_control_number", held, update_modified=False)
		doc.related_control_number = held
		return
	related_cn = generate_control_number(doc.name, related=True)
	doc.db_set("related_control_number", related_cn, update_modified=False)
	doc.related_control_number = related_cn
	if primary_cn and frappe.db.exists("TCB Control Number", primary_cn):
		frappe.db.set_value("TCB Control Number", primary_cn, "related_control_number", related_cn)


def _register_with_tcb(doc, control_number: str):
	"""Register the control number with TCB synchronously during SO submit.

	REGISTER-FIRST GATE: runs inline and BLOCKS the submit if registration fails.
	An unregistered control number is unpayable, so letting the SO through would
	just create a dead order; instead we throw a clear banner and the user retries.
	An already-registered number returns ok=True (see register_reference_for_sales_order),
	so re-submitting after a blocked attempt passes straight through.

	On a project that also collects through Malipopay, one working rail is
	enough: if TCB cannot register but Malipopay can take payment, the order
	goes through and the TCB step is queued under Waiting for TCB (payments.tcb_catch_up),
	finished once TCB is back — automatically when that is switched on, else by Try now.
	"""
	result = register_reference_on_submit(doc.name, control_number)
	if result.get("status") == "Issuer Pending":
		doc.flags.landms_issuer_pending = result
		frappe.msgprint(
			f"The bank (TCB) is not answering right now, so payment number {control_number} is not "
			f"set up there yet. The order is submitted: the customer can pay through Malipopay now. "
			f"The TCB step is listed under Waiting for TCB and is finished there once TCB is back.",
			indicator="orange",
			alert=True,
		)
		return
	if not result.get("ok"):
		frappe.throw(
			f"This order could not be submitted because its payment number could not "
			f"be set up with the bank yet. The bank portal may be temporarily "
			f"unavailable, and the order cannot receive payment until this succeeds. "
			f"Please try submitting again after a while. "
			f"(Ref {control_number}: {result.get('message')})"
		)


def _link_application_to_sales_order(doc):
	app = _get_application(doc, required=False)
	if app and app.sales_order != doc.name:
		app.db_set("sales_order", doc.name)


def _clear_application_sales_order_link(doc):
	app = _get_application(doc, required=False)
	if app and app.sales_order == doc.name:
		app.db_set("sales_order", "")


def _mark_plot_pending_advance(doc):
	if not doc.plot:
		return
	set_plot_status(
		doc.plot,
		"Pending Advance",
		f"sales order {link('Sales Order', doc.name)} was submitted",
		only_from=("Available",),
	)


def _validate_customer_mobile_for_tcb(doc):
	customer = cstr(doc.get("customer") or "").strip()
	if not customer:
		return

	mobile = _get_customer_mobile(customer)
	if is_valid_tcb_mobile(mobile):
		return

	frappe.throw(
		f"Customer {customer} must have a valid phone number in the format "
		"255XXXXXXXXX before this Sales Order can be created. "
		"Update the mobile number on the Customer's primary Contact."
	)


def _get_customer_mobile(customer: str) -> str:
	"""Get mobile from Customer record or its primary Contact."""
	# Try Customer.mobile_no first
	mobile = cstr(frappe.db.get_value("Customer", customer, "mobile_no") or "").strip()
	if mobile:
		return mobile

	# Fall back to primary Contact's mobile_no or phone
	contact = frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": "Customer", "link_name": customer, "parenttype": "Contact"},
		"parent",
	)
	if contact:
		mobile = cstr(frappe.db.get_value("Contact", contact, "mobile_no") or "").strip()
		if not mobile:
			mobile = cstr(frappe.db.get_value("Contact", contact, "phone") or "").strip()
	return mobile


def _block_cancel_if_paid(doc):
	# Termination flow already handles accounting via forfeiture JE
	if doc.flags.get("_from_termination"):
		return
	# Draft-contract forfeiture already handled in before_cancel: safe to cancel.
	if doc.get("forfeiture_entry"):
		return
	plot_invoice = doc.get("plot_sales_invoice")
	if plot_invoice and frappe.db.exists("Sales Invoice", plot_invoice):
		outstanding, grand_total = frappe.db.get_value(
			"Sales Invoice",
			plot_invoice,
			["outstanding_amount", "grand_total"],
		)
		if flt(outstanding) < flt(grand_total):
			# Point to the RIGHT remedy: a still-Draft contract (partial advance) is
			# resolved with the Close button; only a submitted/Ongoing contract uses
			# Terminate Contract. (Previously this always said "Terminate", which is
			# wrong — and unavailable — for a Draft-contract partial-payment order.)
			submitted_contract = frappe.db.get_value(
				"Plot Contract", {"sales_order": doc.name, "docstatus": 1}, "name"
			)
			remedy = (
				"Use Plot Contract → Terminate Contract instead."
				if submitted_contract
				else "Use the Close Sales Order button instead."
			)
			frappe.throw(
				f"Sales Order {doc.name} cannot be cancelled — payment has been "
				f"received against Sales Invoice {plot_invoice}. {remedy}"
			)


def _block_close_if_paid(doc):
	submitted_contract = frappe.db.get_value(
		"Plot Contract",
		{"sales_order": doc.name, "docstatus": 1},
		"name",
	)
	if submitted_contract:
		frappe.throw(
			f"Sales Order {doc.name} cannot be closed — Plot Contract {submitted_contract} "
			"has been submitted. Use Plot Contract → Terminate Contract instead."
		)


def _cancel_unpaid_plot_sales_invoice(doc):
	# Termination flow already handled the SI before calling SO cancel
	if doc.flags.get("_from_termination"):
		return
	# Draft-contract forfeiture posted: SI stays submitted as audit trail,
	# matching the termination pattern for paid invoices.
	if doc.get("forfeiture_entry"):
		return
	invoice_name = doc.get("plot_sales_invoice")
	if not invoice_name or not frappe.db.exists("Sales Invoice", invoice_name):
		return

	invoice = frappe.get_doc("Sales Invoice", invoice_name)
	if invoice.docstatus == 0:
		frappe.delete_doc("Sales Invoice", invoice.name, ignore_permissions=True)
		return

	if invoice.docstatus != 1:
		return

	if flt(invoice.outstanding_amount) < flt(invoice.grand_total):
		# Invoice has payments — leave open as audit trail, credit note will zero the outstanding
		return

	invoice.cancel()


def _delete_draft_plot_contract(doc):
	# Collect candidates: the stored link on the doc + any DB matches by sales_order.
	candidates = set()
	if doc.get("plot_contract") and frappe.db.exists("Plot Contract", doc.plot_contract):
		candidates.add(doc.plot_contract)

	for name in frappe.db.get_all(
		"Plot Contract",
		filters={"sales_order": doc.name, "docstatus": 0},
		pluck="name",
	):
		candidates.add(name)

	for name in candidates:
		if frappe.db.get_value("Plot Contract", name, "docstatus") == 0:
			frappe.delete_doc("Plot Contract", name, ignore_permissions=True, force=True)
			if doc.get("plot_contract") == name:
				doc.db_set("plot_contract", "", update_modified=False)


def _find_draft_plot_contract(doc):
	if doc.get("plot_contract") and frappe.db.exists("Plot Contract", doc.plot_contract):
		if frappe.db.get_value("Plot Contract", doc.plot_contract, "docstatus") == 0:
			return doc.plot_contract
	rows = frappe.db.get_all(
		"Plot Contract",
		filters={"sales_order": doc.name, "docstatus": 0},
		pluck="name",
		limit=1,
	)
	return rows[0] if rows else None


def _post_draft_contract_forfeiture_je(doc):
	"""Post forfeiture JE for payments received before the Plot Contract was submitted.

	Mirrors `plot_contract._post_termination_journal_entry` but runs from the SO
	side when the user cancels an SO whose contract never reached submit. Moves
	`total_paid` from Customer Advance (liability) to Forfeited Deposits (income).
	"""
	if doc.flags.get("_from_termination"):
		return
	if doc.get("forfeiture_entry"):
		return

	contract_name = _find_draft_plot_contract(doc)
	if not contract_name:
		return

	total_paid = flt(frappe.db.get_value("Plot Contract", contract_name, "total_paid") or 0)
	if total_paid <= 0:
		return

	settings = frappe.get_single("LandMS Settings")
	forfeiture_pct = flt(settings.forfeiture_percentage or 100)
	govt_pct = flt(doc.get("government_share_percent") or 0)
	forfeited_amount = total_paid * forfeiture_pct / 100.0
	# The government share was already moved to Government Payable as each payment
	# came in (see payment_sync._post_government_share_je), so only the company's
	# NET take — forfeited amount minus that already-withheld govt share — is new
	# income here. The customer's refund stays parked in Customer Advance for the
	# accountant to pay out manually (tracked via lms_refund_amount).
	govt_withheld = total_paid * govt_pct / 100.0
	forfeiture_income = forfeited_amount - govt_withheld
	refund_amount = total_paid - forfeited_amount
	if forfeiture_income <= 0:
		return

	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"posting_date": today(),
			"company": settings.company,
			"voucher_type": "Journal Entry",
			"lms_refund_amount": refund_amount,
			# the journal points back at its order; the order finds it from there
			"lms_sales_order": doc.name,
			"user_remark": (
				f"Sales Order {doc.name} cancelled before Plot Contract was submitted — "
				f"{forfeiture_pct:.0f}% of paid amount forfeited, "
				f"net of {govt_pct:.2f}% government share already withheld. "
				f"Customer {doc.customer}, Plot {doc.get('plot') or ''}."
			),
			"accounts": [
				{
					"account": settings.customer_advance_account,
					"debit_in_account_currency": forfeiture_income,
					"cost_center": settings.cost_center,
					"land_acquisition": doc.get("land_acquisition") or "",
				},
				{
					"account": settings.forfeited_deposits_account,
					"credit_in_account_currency": forfeiture_income,
					"cost_center": settings.cost_center,
					"land_acquisition": doc.get("land_acquisition") or "",
				},
			],
		}
	)
	je.insert(ignore_permissions=True)
	je.submit()
	doc.db_set("forfeiture_entry", je.name)


def _post_credit_note_for_outstanding(si_name):
	"""Create a return Sales Invoice (credit note) for the unpaid outstanding balance.

	Called after SO close or contract termination when a partial payment existed and
	the original SI was deliberately left open as an audit trail. The credit note
	reverses only the outstanding portion — it does not touch allocated payments.

	Accounting: Dr Customer Advances / Cr AR (mirrors the original SI income account).
	"""
	if not si_name or not frappe.db.exists("Sales Invoice", si_name):
		return None

	si_doc = frappe.get_doc("Sales Invoice", si_name)
	outstanding = flt(si_doc.outstanding_amount)
	if outstanding <= 0:
		return None

	if not si_doc.get("items"):
		return None

	settings = frappe.get_single("LandMS Settings")
	original_item = si_doc.items[0]

	credit_note = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"is_return": 1,
			"return_against": si_name,
			"customer": si_doc.customer,
			"company": si_doc.company,
			"posting_date": today(),
			"due_date": today(),
			"plot": si_doc.get("plot"),
			"land_acquisition": si_doc.get("land_acquisition"),
			"plot_contract": si_doc.get("plot_contract"),
			"is_plot_sale_invoice": 1,
			"update_stock": 0,
			"is_not_vfd_invoice": 1,
			"allocate_advances_automatically": 0,
			"update_outstanding_for_self": 0,
			"remarks": f"Credit note — sale cancelled, reversing outstanding balance on {si_name}",
			"items": [
				{
					"item_code": original_item.item_code,
					"item_name": original_item.item_name,
					"qty": -1,
					"uom": original_item.uom,
					"stock_uom": original_item.stock_uom or original_item.uom,
					"conversion_factor": 1,
					"rate": outstanding,
					"income_account": settings.customer_advance_account,
					"cost_center": settings.cost_center,
					"land_acquisition": si_doc.get("land_acquisition") or "",
					# Point the return line back at the original invoice's item row. ERPNext's
					# return validation (validate_returned_items) keys on (item_code,
					# sales_invoice_item); without this pointer it msgprints a scary — but
					# non-blocking — "Returned Item ... does not exist in Sales Invoice ..." at the
					# operator on every close. Setting it makes the validation match cleanly and the
					# message go away. (Purely cosmetic before; the credit note posted regardless.)
					"sales_invoice_item": original_item.name,
					# csf_tz validate_items_remaining_qty (hooked on ALL Sales Invoice
					# validate, no is_return guard) throws "<item> item balance is ZERO.
					# Cannot proceed unless Allow Over Sell" when the plot item's on-hand
					# qty is 0 — the normal end state of a sold plot. This is a no-stock
					# financial return (update_stock=0), so the sellable-stock guard does
					# not apply. Set per-line (item.allow_over_sell) so the forfeiture
					# credit note can post; NOT the global Stock Settings.allow_negative_stock.
					"allow_over_sell": 1,
				}
			],
		}
	)

	caller = become_administrator()
	try:
		credit_note.insert(ignore_permissions=True)
		credit_note.submit()
	finally:
		restore_caller(caller)

	return credit_note.name


def _cancel_plot_application(doc):
	"""Cascade-cancel the linked Plot Application so the plot is released.

	Uses `from_sales_order_cancel` flag so the PA's own on_cancel does not try
	to cancel this Sales Order back (circular re-entry guard).
	"""
	if doc.flags.get("_from_termination"):
		return  # Termination flow handles PA cancellation on the contract side.
	app_name = doc.get("plot_application")
	if not app_name or not frappe.db.exists("Plot Application", app_name):
		return
	if frappe.db.get_value("Plot Application", app_name, "docstatus") != 1:
		return
	app = frappe.get_doc("Plot Application", app_name)
	app.flags.from_sales_order_cancel = True
	app.flags.ignore_links = True
	app.cancel()


def _release_plot_if_no_active_application(doc):
	if not doc.plot:
		return

	active_application = frappe.db.exists(
		"Plot Application",
		{
			"plot": doc.plot,
			"docstatus": 1,
			"status": ["in", ["Submitted", "Paid", "Converted"]],
		},
	)
	if active_application:
		return

	ended = "cancelled" if doc.docstatus == 2 else "closed"
	set_plot_status(
		doc.plot,
		"Available",
		f"sales order {link('Sales Order', doc.name)} was {ended} and no application holds the plot",
		only_from=("Pending Advance", "Pending Fee"),
	)
