"""The plot contract's payment look-ups without raw SQL (PC-7a).

Two things on a contract come from these look-ups:
  - the paid date of each installment (the advance, then the balance), from the payments on the plot invoice;
  - the "Payment Entries" list on the contract's linked-documents panel, from the payments on the
    sales order and the plot invoice.
They now use Frappe's query builder instead of raw SQL strings. On a copy of live, today's code and
the new code gave identical results for every contract and every invoice a payment points at.

The tests build their own small set of payments (written straight to the tables, no payment logic
runs), because live has no payment made against a sales order, so that path needs its own proof.

Safety (operator, 5 Nov 2026: "we don't want to break the production data"): every write is
rolled back, and a database commit or an outside call (bank, SMS, email gateway) fails the test.
"""

import re
from datetime import date
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms.landms.doctype.plot_contract.plot_contract import _get_plot_payment_entries

SO = "_T-PC7A-SO"
SI = "_T-PC7A-SINV"
OTHER_SI = "_T-PC7A-SINV-OTHER"
DRAFT, SUBMITTED, CANCELLED = 0, 1, 2


def _no_commit():
	raise AssertionError("a database commit happened inside the PC-7a test; nothing may be saved for real")


def _no_outside_call(*args, **kwargs):
	raise AssertionError("an outside call was attempted inside the PC-7a test")


def _payment(name, posting_date, docstatus, lines):
	"""A Payment Entry with reference lines [(doctype, name, amount)], written straight to the tables."""
	pe = frappe.new_doc("Payment Entry")
	pe.name = name
	pe.payment_type = "Receive"
	pe.posting_date = posting_date
	pe.docstatus = docstatus
	pe.db_insert()
	for idx, (reference_doctype, reference_name, amount) in enumerate(lines, start=1):
		frappe.get_doc(
			{
				"doctype": "Payment Entry Reference",
				"name": f"{name}-{idx}",
				"parent": name,
				"parenttype": "Payment Entry",
				"parentfield": "references",
				"idx": idx,
				"docstatus": docstatus,
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"allocated_amount": amount,
			}
		).db_insert()


def _contract(sales_order=None, booking_fee_invoice=None, booking_fee_percent=0):
	contract = frappe.new_doc("Plot Contract")
	contract.sales_order = sales_order
	contract.booking_fee_invoice = booking_fee_invoice
	contract.booking_fee_percent = booking_fee_percent
	return contract


class TestPlotContractNoRawSql(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=_no_outside_call)
		blocker.start()
		self.addCleanup(blocker.stop)

	def test_no_raw_sql_in_the_contract_code(self):
		source = open(
			frappe.get_app_path("landms", "landms", "doctype", "plot_contract", "plot_contract.py")
		).read()
		self.assertFalse(re.search(r"frappe\.db\.(sql|multisql)\b|(^|[^\w])db\.sql\s*\(", source, re.M))

	def test_invoice_payments_are_submitted_lines_oldest_first(self):
		_payment("_T-PC7A-PE-B", "2026-01-10", SUBMITTED, [("Sales Invoice", SI, 100)])
		_payment(
			"_T-PC7A-PE-C", "2026-01-05", SUBMITTED, [("Sales Invoice", SI, 30), ("Sales Invoice", SI, 20)]
		)
		_payment("_T-PC7A-PE-A", "2026-01-05", SUBMITTED, [("Sales Invoice", SI, 5)])  # same day: by name
		_payment("_T-PC7A-PE-D", "2026-01-01", DRAFT, [("Sales Invoice", SI, 999)])
		_payment("_T-PC7A-PE-E", "2026-01-02", CANCELLED, [("Sales Invoice", SI, 999)])
		_payment("_T-PC7A-PE-F", "2026-01-03", SUBMITTED, [("Sales Invoice", OTHER_SI, 999)])
		# same name as the invoice, but a sales-order line: not an invoice payment
		_payment("_T-PC7A-PE-G", "2026-01-04", SUBMITTED, [("Sales Order", SI, 999)])

		events = _contract()._get_invoice_payment_entry_events(SI)

		# every line of a submitted payment on this invoice, oldest first, then by payment name
		self.assertEqual(
			[(e.posting_date, e.name) for e in events],
			[
				(date(2026, 1, 5), "_T-PC7A-PE-A"),
				(date(2026, 1, 5), "_T-PC7A-PE-C"),
				(date(2026, 1, 5), "_T-PC7A-PE-C"),
				(date(2026, 1, 10), "_T-PC7A-PE-B"),
			],
		)
		self.assertEqual(sorted(e.amount for e in events), [5, 20, 30, 100])
		self.assertEqual(set(events[0].keys()), {"posting_date", "amount", "name"})

	def test_installment_paid_dates_follow_the_payments(self):
		# 1,000 invoice, 20% advance: the advance is paid when 200 is reached, the balance at 1,000
		_payment("_T-PC7A-PE-1", "2026-01-01", DRAFT, [("Sales Invoice", SI, 1000)])  # never counts
		_payment("_T-PC7A-PE-2", "2026-01-05", SUBMITTED, [("Sales Invoice", SI, 150)])
		_payment("_T-PC7A-PE-3", "2026-01-10", SUBMITTED, [("Sales Invoice", SI, 100)])
		_payment("_T-PC7A-PE-4", "2026-01-20", CANCELLED, [("Sales Invoice", SI, 750)])  # reversed
		_payment("_T-PC7A-PE-5", "2026-02-01", SUBMITTED, [("Sales Invoice", SI, 750)])
		invoice = frappe._dict(name=SI, grand_total=1000, payment_schedule=[{}, {}], advances=[])

		paid_dates = _contract(booking_fee_percent=20)._get_paid_dates_by_installment(invoice)

		self.assertEqual(paid_dates, {1: date(2026, 1, 10), 2: date(2026, 2, 1)})

	def test_contract_payment_list_has_order_and_invoice_payments_once_each(self):
		_payment("_T-PC7A-PE-1", "2026-01-03", SUBMITTED, [("Sales Order", SO, 100)])
		_payment("_T-PC7A-PE-2", "2026-01-01", SUBMITTED, [("Sales Invoice", SI, 100)])
		_payment(
			"_T-PC7A-PE-3", "2026-01-02", SUBMITTED, [("Sales Order", SO, 50), ("Sales Invoice", SI, 50)]
		)
		_payment(
			"_T-PC7A-PE-4", "2026-01-04", SUBMITTED, [("Sales Invoice", SI, 10), ("Sales Invoice", SI, 10)]
		)
		_payment("_T-PC7A-PE-5", "2026-01-01", DRAFT, [("Sales Order", SO, 999)])
		_payment("_T-PC7A-PE-6", "2026-01-01", CANCELLED, [("Sales Invoice", SI, 999)])
		_payment("_T-PC7A-PE-7", "2026-01-01", SUBMITTED, [("Sales Invoice", OTHER_SI, 999)])

		listed = _get_plot_payment_entries(_contract(sales_order=SO, booking_fee_invoice=SI))

		self.assertEqual(
			[(row.posting_date, row.name) for row in listed],
			[
				(date(2026, 1, 1), "_T-PC7A-PE-2"),
				(date(2026, 1, 2), "_T-PC7A-PE-3"),
				(date(2026, 1, 3), "_T-PC7A-PE-1"),
				(date(2026, 1, 4), "_T-PC7A-PE-4"),
			],
		)
		self.assertEqual(
			[row.name for row in _get_plot_payment_entries(_contract(booking_fee_invoice=SI))],
			["_T-PC7A-PE-2", "_T-PC7A-PE-3", "_T-PC7A-PE-4"],
		)
		self.assertEqual(_get_plot_payment_entries(_contract()), [])
