"""Payment Entry and single-invoice sync helpers for LandMS plot sales.

The first advance may arrive against the Sales Order before the plot SI exists.
When that happens we:
  1. accept the Payment Entry against the SO,
  2. create the single plot SI on submit,
  3. let ERPNext allocate the SO advance onto that SI,
  4. mirror invoice / payment state back to the SO and Plot Contract.

After the SI exists, all later payments are normalized directly onto it.
"""

from __future__ import annotations

import time

import frappe
from frappe.query_builder import Order
from frappe.utils import cstr, flt, today

from landms.elevation import become_administrator, restore_caller
from landms.sales_order_hooks import ensure_plot_sales_invoice_for_sales_order


def validate_payment_entry(doc, method=None):
	"""Normalize plot payments onto the single plot SI once it exists."""
	if doc.docstatus != 0:
		return
	if doc.payment_type != "Receive" or doc.party_type != "Customer":
		return
	if not doc.get("references"):
		return

	if doc.contact_person and not doc.mobile_no:
		contact_info = frappe.get_cached_value(
			"Contact", doc.contact_person, ["mobile_no", "phone"], as_dict=True
		)
		doc.mobile_no = contact_info.get("mobile_no") or contact_info.get("phone")

	normalized_rows = []
	seen_invoice_rows: dict[tuple[str, str], dict] = {}

	for row in doc.get("references") or []:
		target = _resolve_landms_plot_reference(
			row.reference_doctype,
			row.reference_name,
			create_missing_invoice=False,
		)
		if not target or not target.get("invoice_name"):
			normalized_rows.append(_reference_row_to_dict(row))
			continue

		invoice_name = target["invoice_name"]
		payment_term = cstr(getattr(row, "payment_term", "") or "").strip()
		key = (invoice_name, payment_term)
		current_amount = flt(row.allocated_amount)
		existing = seen_invoice_rows.get(key)

		if not doc.land_acquisition and target.get("land_acquisition"):
			doc.land_acquisition = target.get("land_acquisition")

		if existing:
			existing["allocated_amount"] = flt(existing.get("allocated_amount")) + current_amount
			existing["outstanding_amount"] = flt(target["outstanding_amount"])
			existing["total_amount"] = flt(target["grand_total"])
			existing["due_date"] = target.get("due_date")
			existing["payment_term_outstanding"] = flt(
				getattr(row, "payment_term_outstanding", 0) or existing.get("payment_term_outstanding") or 0
			)
			existing["exchange_rate"] = (
				flt(getattr(row, "exchange_rate", 0) or existing.get("exchange_rate") or 0) or 1
			)
		else:
			seen_invoice_rows[key] = {
				"reference_doctype": "Sales Invoice",
				"reference_name": invoice_name,
				"allocated_amount": current_amount,
				"total_amount": flt(target["grand_total"]),
				"outstanding_amount": flt(target["outstanding_amount"]),
				"due_date": target.get("due_date"),
				"payment_term": payment_term or None,
				"payment_term_outstanding": flt(getattr(row, "payment_term_outstanding", 0) or 0),
				"exchange_rate": flt(getattr(row, "exchange_rate", 0) or 0) or 1,
			}

	doc.set("references", [])
	for row_dict in normalized_rows:
		doc.append("references", row_dict)
	for row_dict in seen_invoice_rows.values():
		doc.append("references", row_dict)


def on_submit_payment_entry(doc, method=None):
	_ensure_first_advance_plot_invoices(doc)
	_post_government_share_je(doc)
	_enqueue_plot_payment_sync(doc, "submit")


def on_cancel_payment_entry(doc, method=None):
	_cancel_government_share_je(doc)
	_enqueue_plot_payment_sync(doc, "cancel")


def sync_plot_contract_from_payment_entry(doc, method=None):
	"""Backward-compatible hook name kept for older wiring."""
	_sync_landms_payment_entry_state(doc)


def _enqueue_plot_payment_sync(doc, event):
	"""Run payment sync synchronously for each related plot SO.
	Only fires for plot sale invoices — safe to call on any Payment Entry.
	"""
	related = _get_related_landms_documents_from_payment_entry(doc)
	for so_name in sorted(related["sales_orders"]):
		try:
			_run_plot_payment_sync(so_name, doc)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"plot_pay sync failed for {so_name}")


def _run_plot_payment_sync(so_name, pe_doc):
	"""Sync SO + SI + Contract for one plot Sales Order.
	Only runs for is_plot_sale_invoice SOs — safe to ignore all others.
	"""
	if not frappe.db.exists("Sales Order", so_name):
		return

	inv_name = frappe.db.get_value("Sales Order", so_name, "plot_sales_invoice")
	if not inv_name:
		return
	if not frappe.db.get_value("Sales Invoice", inv_name, "is_plot_sale_invoice"):
		return

	_sync_sales_order_from_plot_invoice(so_name, pe_doc)
	_sync_si_schedule_for_so(so_name, inv_name)

	contract_name = frappe.db.get_value("Sales Order", so_name, "plot_contract")
	if contract_name and frappe.db.exists("Plot Contract", contract_name):
		_sync_contract_after_payment(contract_name, pe_name=pe_doc.name)


def _sync_si_schedule_for_so(so_name, inv_name):
	"""Sync the Sales Invoice payment schedule rows using the SO's booking_fee_percent."""
	booking_fee_pct = flt(frappe.db.get_value("Sales Order", so_name, "booking_fee_percent") or 0)
	invoice = frappe.get_doc("Sales Invoice", inv_name)
	if not getattr(invoice, "is_plot_sale_invoice", 0):
		return
	_sync_payment_schedule_rows_from_invoice(invoice, invoice, booking_fee_percent=booking_fee_pct)


def _sync_contract_after_payment(contract_name, pe_name=None):
	"""Sync Plot Contract payment status after a payment.
	Reads pe.unallocated_amount to detect overpayment (only for plot sale PEs).
	"""
	contract = frappe.get_doc("Plot Contract", contract_name)
	contract.sync_payment_status()

	# Overpayment: if the PE had more money than the SI outstanding, the
	# excess sits in pe.unallocated_amount — show it on the contract.
	if pe_name:
		unallocated = flt(frappe.db.get_value("Payment Entry", pe_name, "unallocated_amount") or 0)
		if unallocated > 0:
			frappe.db.set_value(
				"Plot Contract",
				contract_name,
				"overpaid_amount",
				unallocated,
				update_modified=False,
			)


def _build_pe_references_for_invoice(si, total_allocated: float) -> list[dict]:
	"""Build PE reference rows that allocate payment across SI payment schedule terms.

	ERPNext only updates per-row paid_amount on the SI payment schedule when the
	PE reference row carries the matching payment_term. Without it, the overall
	SI outstanding updates but individual schedule rows stay at zero — breaking
	our contract sync which reads from those rows.
	"""
	schedule = si.get("payment_schedule") or []
	if not schedule:
		return [
			{
				"reference_doctype": "Sales Invoice",
				"reference_name": si.name,
				"allocated_amount": flt(total_allocated),
			}
		]

	refs = []
	remaining = flt(total_allocated)
	for row in sorted(schedule, key=lambda r: r.idx):
		if remaining <= 0:
			break
		row_outstanding = flt(row.outstanding or 0)
		if row_outstanding <= 0:
			continue
		alloc = min(remaining, row_outstanding)
		refs.append(
			{
				"reference_doctype": "Sales Invoice",
				"reference_name": si.name,
				"allocated_amount": alloc,
				"payment_term": row.payment_term,
			}
		)
		remaining -= alloc

	# If there's still remaining (overpayment or no matching terms), add a catch-all row
	if remaining > 0:
		refs.append(
			{
				"reference_doctype": "Sales Invoice",
				"reference_name": si.name,
				"allocated_amount": remaining,
			}
		)

	return refs or [
		{
			"reference_doctype": "Sales Invoice",
			"reference_name": si.name,
			"allocated_amount": flt(total_allocated),
		}
	]


def create_payment_entry_for_sales_order(
	*,
	sales_order_name: str,
	amount: float,
	payment_date: str | None,
	bank_account: str,
	reference_no: str,
	remarks: str | None = None,
	submit: bool = True,
	name_from_reference: bool = True,
) -> str:
	"""Create a Payment Entry against the plot SI, optionally submitting it.

	Used by TCB inbound, so if this is the first advance we create the single
	plot SI on demand before posting the PE. When `submit=False` the PE is
	inserted as a Draft for manual review instead of being auto-submitted.
	`name_from_reference` keeps the rail convention of naming the PE with the
	provider's reference; callers pass False for a reference that is not safe
	as a primary key, and ERPNext names the entry normally instead.
	"""
	if not sales_order_name or not frappe.db.exists("Sales Order", sales_order_name):
		frappe.throw(f"Sales Order {sales_order_name} not found.")
	if flt(amount) <= 0:
		frappe.throw("Payment amount must be greater than zero.")
	if not bank_account:
		frappe.throw("Bank/Cash account is required to create the Payment Entry.")
	if not reference_no:
		frappe.throw("Payment reference number is required (used for idempotency).")

	# One payment per order at a time, whichever rail it came from (TCB notice,
	# TCB reconciliation, Malipopay booking). Without this, two payments landing
	# together both read the same balance and the invoice loses one of them.
	_lock_sales_order_for_payment(sales_order_name)

	so = frappe.get_doc("Sales Order", sales_order_name)
	if so.docstatus != 1:
		frappe.throw(f"Sales Order {sales_order_name} is not submitted.")
	_refuse_if_read_before_another_payment(
		"Sales Order", sales_order_name, "plot_sales_invoice", so.get("plot_sales_invoice")
	)

	invoice_name = so.get("plot_sales_invoice") or ensure_plot_sales_invoice_for_sales_order(
		sales_order_name,
		posting_date=payment_date or today(),
	)
	if not invoice_name or not frappe.db.exists("Sales Invoice", invoice_name):
		frappe.throw(f"Sales Order {sales_order_name} has no plot Sales Invoice yet. Cannot apply payment.")

	if _payment_reference_exists(invoice_name, reference_no):
		frappe.throw(f"Duplicate payment reference {reference_no} for Sales Invoice {invoice_name}.")

	si = frappe.get_doc("Sales Invoice", invoice_name)
	if si.docstatus != 1:
		frappe.throw(f"Sales Invoice {invoice_name} is not submitted.")
	_refuse_if_read_before_another_payment(
		"Sales Invoice", invoice_name, "outstanding_amount", si.outstanding_amount
	)

	outstanding = flt(si.outstanding_amount)
	if outstanding <= 0:
		frappe.throw(f"Sales Invoice {invoice_name} has no outstanding balance. Nothing to apply.")

	allocated = min(flt(amount), outstanding)
	pe_references = _build_pe_references_for_invoice(si, allocated)

	pe = frappe.get_doc(
		{
			"doctype": "Payment Entry",
			"payment_type": "Receive",
			"posting_date": payment_date or today(),
			"company": si.company,
			"party_type": "Customer",
			"party": si.customer,
			"paid_from": si.debit_to,
			"paid_to": bank_account,
			"paid_amount": flt(amount),
			"received_amount": flt(amount),
			"reference_no": cstr(reference_no),
			"reference_date": payment_date or today(),
			"remarks": remarks or f"Payment against {invoice_name}",
			"references": pe_references,
		}
	)
	if name_from_reference:
		pe.name = cstr(reference_no)
		pe.flags.name_set = True

	caller = become_administrator()
	try:
		pe.insert(ignore_permissions=True, set_name=cstr(reference_no) if name_from_reference else None)
		if submit:
			pe.submit()
	finally:
		restore_caller(caller)
	return pe.name


class ConcurrentPaymentError(frappe.ValidationError):
	"""Another payment on the same order was recorded while this one was being
	prepared. Nothing of this one is saved. The rails go through
	record_payment_waiting_for_others, which waits and records it again with
	fresh figures — TCB never resends a notice (its endpoint always answers 200)."""


# How many times a rail's payment is tried when another payment on the same
# order is being recorded at the same moment.
CONCURRENT_ATTEMPTS = 3
# Three waits of 30 s stay inside the web server's 120 s limit for a TCB notice.
PAYMENT_LOCK_WAIT_SECONDS = 30
PAYMENT_LOCK_RETRY_SECONDS = 0.2


def record_payment_waiting_for_others(**kwargs):
	"""The rails' way to record a payment (TCB notice and reconciliation,
	Malipopay booking): if another payment on the same order was being recorded
	at the same moment, wait for it to finish and record this one with fresh
	figures, instead of refusing it (proven 14 Oct 2026: the second of two
	simultaneous TCB notices was refused and would only have been picked up by
	the midnight reconciliation).

	Between attempts the transaction is committed: that is what makes the next
	attempt read the other payment's figures, and nothing of the refused attempt
	was written. The rails already commit as they go (the TCB logger does, the
	Malipopay booking owns its commits); console scripts call
	create_payment_entry_for_sales_order directly and keep rehearse-then-rollback."""
	for attempt in range(1, CONCURRENT_ATTEMPTS + 1):
		try:
			return create_payment_entry_for_sales_order(**kwargs)
		except ConcurrentPaymentError:
			if attempt == CONCURRENT_ATTEMPTS:
				raise
			frappe.db.commit()  # ends this transaction: the next attempt starts a fresh read


def _lock_sales_order_for_payment(sales_order_name):
	"""Hold the order's row until this transaction ends; a second payment on the order waits here."""
	deadline = time.monotonic() + PAYMENT_LOCK_WAIT_SECONDS
	while True:
		try:
			frappe.db.get_value("Sales Order", sales_order_name, "name", for_update=True, wait=False)
			return
		except frappe.QueryTimeoutError:
			if time.monotonic() >= deadline:
				raise ConcurrentPaymentError(
					f"Another payment on Sales Order {sales_order_name} is still being recorded. "
					"This payment was not saved and will be tried again."
				) from None
			time.sleep(PAYMENT_LOCK_RETRY_SECONDS)


def _refuse_if_read_before_another_payment(doctype, name, fieldname, value_read):
	"""Refuse when this transaction read a figure that another payment has
	since changed. The database answers ordinary reads from the moment the
	transaction began, so waiting on the lock alone is not enough: the
	locking read sees the latest saved value, and if it differs, every figure
	this payment would be built on is out of date."""
	current = frappe.db.get_value(doctype, name, fieldname, for_update=True)
	changed = (
		abs(flt(current) - flt(value_read)) >= 0.005
		if fieldname == "outstanding_amount"
		else cstr(current) != cstr(value_read)
	)
	if changed:
		raise ConcurrentPaymentError(
			f"Another payment on {doctype} {name} was recorded at the same moment. "
			"This payment was not saved and will be tried again."
		)


def _ensure_first_advance_plot_invoices(doc):
	for row in doc.get("references") or []:
		if row.reference_doctype != "Sales Order" or not row.reference_name:
			continue

		so_info = frappe.db.get_value(
			"Sales Order",
			row.reference_name,
			["name", "docstatus", "plot", "plot_sales_invoice"],
			as_dict=True,
		)
		if not so_info or so_info.docstatus != 1 or not so_info.plot or so_info.plot_sales_invoice:
			continue

		ensure_plot_sales_invoice_for_sales_order(
			so_info.name,
			posting_date=doc.posting_date or today(),
		)


def _sync_landms_payment_entry_state(doc):
	related = _get_related_landms_documents_from_payment_entry(doc)

	for sales_order_name in sorted(related["sales_orders"]):
		_sync_sales_order_from_plot_invoice(sales_order_name)

	for contract_name in sorted(related["contracts"]):
		if not frappe.db.exists("Plot Contract", contract_name):
			continue
		contract = frappe.get_doc("Plot Contract", contract_name)
		contract.sync_payment_status()


def _get_related_landms_documents_from_payment_entry(doc) -> dict[str, set[str]]:
	sales_orders = set()
	contracts = set()

	for row in doc.get("references") or []:
		target = _resolve_landms_plot_reference(
			row.reference_doctype,
			row.reference_name,
			create_missing_invoice=False,
		)
		if not target:
			continue

		if target.get("sales_order_name"):
			sales_orders.add(target["sales_order_name"])
		if target.get("plot_contract"):
			contracts.add(target["plot_contract"])

	return {
		"sales_orders": sales_orders,
		"contracts": contracts,
	}


def _resolve_landms_plot_reference(
	reference_doctype: str | None,
	reference_name: str | None,
	*,
	create_missing_invoice: bool = False,
):
	if not reference_doctype or not reference_name:
		return None

	if reference_doctype == "Sales Order":
		if not frappe.db.exists("Sales Order", reference_name):
			return None

		so = frappe.db.get_value(
			"Sales Order",
			reference_name,
			["name", "plot", "plot_sales_invoice", "plot_contract", "land_acquisition"],
			as_dict=True,
		)
		if not so or not so.plot:
			return None

		invoice_name = _get_plot_invoice_name_from_sales_order(reference_name)
		if not invoice_name and create_missing_invoice:
			invoice_name = ensure_plot_sales_invoice_for_sales_order(reference_name)
		if not invoice_name or not frappe.db.exists("Sales Invoice", invoice_name):
			return {
				"sales_order_name": so.name,
				"invoice_name": "",
				"plot_contract": so.plot_contract or "",
				"grand_total": 0,
				"outstanding_amount": 0,
				"due_date": None,
				"land_acquisition": so.land_acquisition or "",
			}

		invoice = frappe.db.get_value(
			"Sales Invoice",
			invoice_name,
			[
				"name",
				"is_plot_sale_invoice",
				"grand_total",
				"outstanding_amount",
				"due_date",
				"plot_contract",
			],
			as_dict=True,
		)
		if not invoice or not invoice.is_plot_sale_invoice:
			return None

		return {
			"sales_order_name": so.name,
			"invoice_name": invoice.name,
			"plot_contract": invoice.plot_contract or so.plot_contract or "",
			"grand_total": invoice.grand_total,
			"outstanding_amount": invoice.outstanding_amount,
			"due_date": invoice.due_date,
			"land_acquisition": so.land_acquisition or "",
		}

	if reference_doctype == "Sales Invoice":
		if not frappe.db.exists("Sales Invoice", reference_name):
			return None

		invoice = frappe.db.get_value(
			"Sales Invoice",
			reference_name,
			[
				"name",
				"is_plot_sale_invoice",
				"grand_total",
				"outstanding_amount",
				"due_date",
				"plot_contract",
				"land_acquisition",
			],
			as_dict=True,
		)
		if not invoice or not invoice.is_plot_sale_invoice:
			return None

		sales_order_name = _get_sales_order_name_from_plot_invoice(reference_name)
		return {
			"sales_order_name": sales_order_name or "",
			"invoice_name": invoice.name,
			"plot_contract": invoice.plot_contract
			or frappe.db.get_value("Sales Order", sales_order_name, "plot_contract")
			or "",
			"grand_total": invoice.grand_total,
			"outstanding_amount": invoice.outstanding_amount,
			"due_date": invoice.due_date,
			"land_acquisition": invoice.land_acquisition or "",
		}

	return None


def _reference_row_to_dict(row):
	return {
		"reference_doctype": row.reference_doctype,
		"reference_name": row.reference_name,
		"allocated_amount": flt(row.allocated_amount),
		"total_amount": flt(getattr(row, "total_amount", 0) or 0),
		"outstanding_amount": flt(getattr(row, "outstanding_amount", 0) or 0),
		"due_date": getattr(row, "due_date", None),
		"payment_term": getattr(row, "payment_term", None),
		"payment_term_outstanding": flt(getattr(row, "payment_term_outstanding", 0) or 0),
		"exchange_rate": flt(getattr(row, "exchange_rate", 0) or 0) or 1,
	}


def _sync_sales_order_from_plot_invoice(sales_order_name: str, pe_doc):
	if not sales_order_name or not frappe.db.exists("Sales Order", sales_order_name):
		return

	so = frappe.get_doc("Sales Order", sales_order_name)
	invoice_name = _get_plot_invoice_name_from_sales_order(sales_order_name)
	if not invoice_name or not frappe.db.exists("Sales Invoice", invoice_name):
		return

	invoice = frappe.get_doc("Sales Invoice", invoice_name)
	if not getattr(invoice, "is_plot_sale_invoice", 0):
		return

	plot_number = frappe.get_cached_value("Plot Master", so.plot, "plot_number")
	pe_doc.db_set("plot", so.plot)
	pe_doc.db_set("plot_number", plot_number)
	pe_doc.db_set("plot_outstanding_amount", invoice.outstanding_amount)
	pe_doc.run_notifications("on_change")

	total_paid = max(0.0, flt(invoice.grand_total) - flt(invoice.outstanding_amount))
	_sync_sales_order_billing_from_plot_invoice(so, invoice)
	_sync_payment_schedule_rows_from_invoice(so, invoice)

	if so.get("plot_sales_invoice") != invoice.name:
		frappe.db.set_value("Sales Order", so.name, "plot_sales_invoice", invoice.name, update_modified=False)

	frappe.db.set_value(
		"Sales Order",
		so.name,
		{
			"advance_paid": flt(total_paid, so.precision("advance_paid")),
			"plot_outstanding_amount": flt(invoice.outstanding_amount),
		},
		update_modified=True,
	)
	frappe.clear_document_cache("Sales Order", so.name)


def _sync_sales_order_billing_from_plot_invoice(so, invoice):
	if not so.items:
		return

	_link_invoice_items_to_sales_order_rows(so, invoice)
	invoice.reload()
	invoice.update_status_updater_args()
	invoice.update_prevdoc_status()
	so.reload()

	total_amount = sum(abs(flt(row.amount)) for row in so.items)
	billed_amount = sum(
		min(
			abs(flt(row.amount)),
			abs(flt(frappe.db.get_value("Sales Order Item", row.name, "billed_amt") or 0)),
		)
		for row in so.items
	)
	per_billed = round((billed_amount / total_amount) * 100, 6) if total_amount else 0.0

	if per_billed < 0.001:
		billing_status = "Not Billed"
	elif per_billed > 99.999999:
		billing_status = "Fully Billed"
	else:
		billing_status = "Partly Billed"

	frappe.db.set_value(
		"Sales Order",
		so.name,
		{
			"per_billed": per_billed,
			"billing_status": billing_status,
		},
		update_modified=True,
	)

	so.reload()
	so.set_status(update=True, update_modified=True)


def _link_invoice_items_to_sales_order_rows(so, invoice):
	so_items = so.get("items") or []
	if not so_items:
		return

	default_so_item = so_items[0].name
	so_item_by_code = {row.item_code: row.name for row in so_items if row.item_code}

	for item in invoice.get("items") or []:
		updates = {}
		if item.sales_order != so.name:
			updates["sales_order"] = so.name

		target_so_detail = item.so_detail or so_item_by_code.get(item.item_code) or default_so_item
		if target_so_detail and item.so_detail != target_so_detail:
			updates["so_detail"] = target_so_detail

		if updates:
			frappe.db.set_value("Sales Invoice Item", item.name, updates, update_modified=False)


def _sync_payment_schedule_rows_from_invoice(parent_doc, invoice, booking_fee_percent=None):
	"""Sync SO payment schedule rows using booking_fee_percent and SI header
	outstanding_amount as the source of truth.

	SI row-level paid_amount/outstanding are not trusted because ERPNext's
	set_payment_schedule resets them on every SI validate. The SI header
	outstanding_amount is always correct regardless of row state.
	booking_fee_percent on the SO never drifts — it is set once at SO creation
	from the Land Acquisition and never modified.
	"""
	source_rows = invoice.get("payment_schedule") or []
	target_rows = parent_doc.get("payment_schedule") or []
	if not target_rows:
		return

	grand_total = flt(invoice.grand_total)
	if grand_total <= 0:
		return

	total_paid = max(0.0, grand_total - flt(invoice.outstanding_amount))
	if booking_fee_percent is None:
		booking_fee_percent = flt(parent_doc.get("booking_fee_percent") or 0)
	else:
		booking_fee_percent = flt(booking_fee_percent)

	# Single row — full amount, no advance split
	if len(target_rows) == 1 or booking_fee_percent <= 0 or booking_fee_percent >= 100:
		due_date = source_rows[0].due_date if source_rows else target_rows[0].due_date
		outstanding = max(0.0, grand_total - total_paid)
		frappe.db.set_value(
			"Payment Schedule",
			target_rows[0].name,
			{
				"due_date": due_date,
				"invoice_portion": 100.0,
				"payment_amount": grand_total,
				"base_payment_amount": grand_total,
				"paid_amount": total_paid,
				"outstanding": outstanding,
				"base_paid_amount": total_paid,
				"base_outstanding": outstanding,
			},
			update_modified=True,
		)
		return

	# Two rows: Row 1 = Advance, Row 2 = Balance
	advance_amount = flt(grand_total * booking_fee_percent / 100)
	balance_amount = flt(grand_total - advance_amount)
	balance_percent = flt(100.0 - booking_fee_percent)

	row1_paid = min(total_paid, advance_amount)
	row1_outstanding = max(0.0, advance_amount - row1_paid)
	row2_paid = max(0.0, total_paid - row1_paid)
	row2_outstanding = max(0.0, balance_amount - row2_paid)

	due_date_1 = source_rows[0].due_date if source_rows else target_rows[0].due_date
	frappe.db.set_value(
		"Payment Schedule",
		target_rows[0].name,
		{
			"due_date": due_date_1,
			"invoice_portion": booking_fee_percent,
			"payment_amount": advance_amount,
			"base_payment_amount": advance_amount,
			"paid_amount": row1_paid,
			"outstanding": row1_outstanding,
			"base_paid_amount": row1_paid,
			"base_outstanding": row1_outstanding,
		},
		update_modified=True,
	)

	if len(target_rows) >= 2:
		due_date_2 = source_rows[1].due_date if len(source_rows) > 1 else target_rows[1].due_date
		frappe.db.set_value(
			"Payment Schedule",
			target_rows[1].name,
			{
				"due_date": due_date_2,
				"invoice_portion": balance_percent,
				"payment_amount": balance_amount,
				"base_payment_amount": balance_amount,
				"paid_amount": row2_paid,
				"outstanding": row2_outstanding,
				"base_paid_amount": row2_paid,
				"base_outstanding": row2_outstanding,
			},
			update_modified=True,
		)


def _get_plot_invoice_name_from_sales_order(sales_order_name: str) -> str:
	invoice_name = frappe.db.get_value("Sales Order", sales_order_name, "plot_sales_invoice")
	if invoice_name and frappe.db.exists("Sales Invoice", invoice_name):
		return invoice_name

	si, item = frappe.qb.DocType("Sales Invoice"), frappe.qb.DocType("Sales Invoice Item")
	result = (
		frappe.qb.from_(si)
		.inner_join(item)
		.on(item.parent == si.name)
		.select(si.name)
		.where(si.docstatus != 2)
		.where(si.is_return == 0)
		.where(si.is_plot_sale_invoice == 1)
		.where(item.sales_order == sales_order_name)
		.orderby(si.modified, order=Order.desc)
		.limit(1)
		.run(as_dict=True)
	)
	return result[0].name if result else ""


def _get_sales_order_name_from_plot_invoice(invoice_name: str) -> str:
	sales_order_name = frappe.db.get_value(
		"Sales Order",
		{"plot_sales_invoice": invoice_name, "docstatus": 1},
		"name",
	)
	if sales_order_name:
		return sales_order_name

	orders = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": invoice_name, "sales_order": ["is", "set"]},
		pluck="sales_order",
		order_by="idx asc",
		limit=1,
	)
	return orders[0] if orders else ""


def _payment_reference_exists(invoice_name: str, reference_no: str) -> bool:
	pe, ref = frappe.qb.DocType("Payment Entry"), frappe.qb.DocType("Payment Entry Reference")
	rows = (
		frappe.qb.from_(pe)
		.inner_join(ref)
		.on(ref.parent == pe.name)
		.select(pe.name)
		.where(pe.docstatus == 1)
		.where(pe.reference_no == reference_no)
		.where(ref.reference_doctype == "Sales Invoice")
		.where(ref.reference_name == invoice_name)
		.limit(1)
		.run()
	)
	return bool(rows)


# ---------------------------------------------------------------------- #
#  Government share JE                                                     #
# ---------------------------------------------------------------------- #


def _post_government_share_je(pe_doc):
	"""Post government share JE for each plot sale payment.

	Dr Customer Advances Account  (reducing deferred revenue)
	Cr Government Payable Account (recording govt share payable)
	Amount = government_share_percent × allocated_amount

	allocated_amount (not the PE's total paid_amount) is used because a single
	payment can bundle in extra cash beyond the plot price — e.g. customers
	often pay government fees alongside the plot price in the same transaction.
	Only the portion actually allocated to the plot sale is land-sale revenue
	subject to the government's share.
	"""
	if pe_doc.docstatus != 1:
		return

	so_name, govt_pct, land_acquisition, allocated_amount = _get_govt_share_fields_from_pe(pe_doc)
	if not so_name or flt(govt_pct) <= 0:
		return

	settings = frappe.get_single("LandMS Settings")
	if not settings.government_payable_account or not settings.customer_advance_account:
		return

	govt_amount = flt(allocated_amount) * flt(govt_pct) / 100.0
	if govt_amount <= 0:
		return

	# The government share leaves GVA's bank to the government at the moment of
	# each payment — GVA does not hold it. So the JE both ACCRUES the liability
	# (rows 1-2) and SETTLES it out of the bank (rows 3-4) in one entry:
	#
	#   Dr Customer Advances      (reduce deferred revenue)
	#   Cr Government Fee Payable  (accrue the govt liability)
	#   Dr Government Fee Payable  (settle the liability...)
	#   Cr Bank                    (...money leaves to government)
	#
	# Government Fee Payable nets to zero within the JE (accrued then settled the
	# same instant) — correct, because GVA holds no residual govt liability. The
	# net cash effect is Dr Advances / Cr Bank for the govt share.
	#
	# The settlement rows require the bank account. If it is not configured we
	# fall back to the accrual-only 2-row JE (previous behaviour) rather than
	# throw — a misconfiguration must not block a customer payment.
	# The share is settled out of the bank the money arrived in. A Malipopay
	# receipt is paid into LandMS Settings.malipopay_bank_account, so its
	# settlement rows credit that account — the same 4-line entry as TCB with
	# the right bank. Every other plot payment keeps the TCB bank, as before.
	bank_account = settings.tcb_bank_account
	malipopay_bank_account = settings.get("malipopay_bank_account")
	if malipopay_bank_account and pe_doc.paid_to == malipopay_bank_account:
		bank_account = malipopay_bank_account
	settle_govt_share = bool(bank_account)

	accounts = [
		{
			"account": settings.customer_advance_account,
			"debit_in_account_currency": govt_amount,
			"cost_center": settings.cost_center,
			"land_acquisition": land_acquisition or "",
		},
		{
			"account": settings.government_payable_account,
			"credit_in_account_currency": govt_amount,
			"cost_center": settings.cost_center,
			"land_acquisition": land_acquisition or "",
		},
	]

	if settle_govt_share:
		accounts.append(
			{
				"account": settings.government_payable_account,
				"debit_in_account_currency": govt_amount,
				"cost_center": settings.cost_center,
				"land_acquisition": land_acquisition or "",
			}
		)
		accounts.append(
			{
				"account": bank_account,
				"credit_in_account_currency": govt_amount,
				"cost_center": settings.cost_center,
				"land_acquisition": land_acquisition or "",
			}
		)

	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"posting_date": pe_doc.posting_date or today(),
			"company": pe_doc.company,
			"voucher_type": "Journal Entry",
			"lms_payment_entry": pe_doc.name,
			"user_remark": (
				f"Government share {flt(govt_pct):.2f}% on payment {pe_doc.name} "
				f"— Sales Order {so_name}"
				+ (
					" (accrued + remitted to government)"
					if settle_govt_share
					else " (accrued only — bank account not configured)"
				)
			),
			"accounts": accounts,
		}
	)
	je.insert(ignore_permissions=True)
	je.submit()


def _cancel_government_share_je(pe_doc):
	"""Cancel the government share JE linked to this Payment Entry."""
	je_name = frappe.db.get_value(
		"Journal Entry",
		{"lms_payment_entry": pe_doc.name, "docstatus": 1},
		"name",
	)
	if not je_name:
		return
	je = frappe.get_doc("Journal Entry", je_name)
	je.cancel()


def _get_govt_share_fields_from_pe(pe_doc):
	"""Return (sales_order_name, government_share_percent, land_acquisition, allocated_amount)
	from PE references.

	allocated_amount is the SUM of allocated amounts across every reference row that
	resolves to the plot sale — its Sales Order directly, or a Sales Invoice flagged
	as the plot sale invoice for that Sales Order. Frappe can split one payment's
	allocation to the same invoice/order across multiple reference rows, so a single
	matching row is not enough. This is not the Payment Entry's total paid_amount,
	which can include extra cash the customer bundled into the same transaction (e.g.
	for government fees) that was never allocated to the plot sale invoice/order."""
	so_name = None
	govt_pct = 0
	land_acquisition = ""
	allocated_amount = 0.0

	for row in pe_doc.get("references") or []:
		row_so_name = None

		if row.reference_doctype == "Sales Invoice":
			si = frappe.db.get_value(
				"Sales Invoice",
				row.reference_name,
				["is_plot_sale_invoice", "plot_contract"],
				as_dict=True,
			)
			if not si or not si.is_plot_sale_invoice:
				continue
			row_so_name = frappe.db.get_value(
				"Sales Invoice Item",
				{"parent": row.reference_name},
				"sales_order",
			)
		elif row.reference_doctype == "Sales Order":
			row_so_name = row.reference_name

		if not row_so_name:
			continue

		if so_name is None:
			so_name = row_so_name
			govt_pct, land_acquisition = frappe.db.get_value(
				"Sales Order",
				so_name,
				["government_share_percent", "land_acquisition"],
			) or (0, "")

		if row_so_name == so_name:
			allocated_amount += flt(row.allocated_amount)

	if not so_name:
		return None, 0, "", 0

	return so_name, flt(govt_pct), land_acquisition, allocated_amount
