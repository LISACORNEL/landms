"""The plot contract's life, tested on real records (PC-7b).

A contract is made by its Sales Order and follows the plot invoice: it submits itself once the advance is
paid, completes when nothing is owed, steps back when a payment is reversed, and Terminate ends it for good.
The operator's rule (7 Nov 2026): the order, the invoice and the contract must agree on the money at every
step, after a payment and after a reversal.

Each test picks a real contract in the right state from the site (skipped when there is none) and pays
through the same function the bank rails use. On a copy of live every test runs.

Safety (operator, 5 Nov 2026: "we don't want to break the production data"): every write is rolled back,
and a database commit or an outside call (bank, SMS, email gateway) fails the test.
"""

from datetime import date, timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, today

from landms.payment_sync import create_payment_entry_for_sales_order


def _no_commit():
	raise AssertionError("a database commit happened inside the PC-7b test; nothing may be saved for real")


def _no_outside_call(*args, **kwargs):
	raise AssertionError("an outside call was attempted inside the PC-7b test")


def _open_order(contract):
	so = frappe.db.get_value("Sales Order", contract.sales_order, ["docstatus", "status"], as_dict=True)
	return so and so.docstatus == 1 and so.status not in ("Closed", "Cancelled", "Completed")


def _invoice(contract):
	name = frappe.db.get_value("Sales Order", contract.sales_order, "plot_sales_invoice")
	if name:
		si = frappe.db.get_value(
			"Sales Invoice", name, ["name", "docstatus", "grand_total", "outstanding_amount"], as_dict=True
		)
		if si and si.docstatus == 1:
			return si
	return None


def _paid(si):
	return flt(si.grand_total) - flt(si.outstanding_amount)


def _advance(contract, si):
	return flt(flt(si.grand_total) * flt(contract.booking_fee_percent) / 100)


def _find(filters, wanted):
	for name in frappe.get_all("Plot Contract", filters=filters, pluck="name", order_by="name desc"):
		contract = frappe.get_doc("Plot Contract", name)
		if contract.sales_order and _open_order(contract) and wanted(contract):
			return contract
	return None


def _pay(contract, amount, ref):
	return create_payment_entry_for_sales_order(
		sales_order_name=contract.sales_order,
		amount=amount,
		payment_date=None,
		bank_account=frappe.db.get_single_value("LandMS Settings", "tcb_bank_account"),
		reference_no=ref,
	)


def _ongoing_with_balance(contract):
	si = _invoice(contract)
	return (
		si
		and flt(si.outstanding_amount) > 0
		and frappe.db.get_value("Plot Master", contract.plot, "status") == "Reserved"
		and not frappe.db.exists("Plot Handover", {"contract": contract.name, "docstatus": ["!=", 2]})
	)


class TestPlotContract(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		for target, new in (
			("requests.sessions.Session.request", _no_outside_call),
			# The bench's csf_tz build stops every new invoice (credit notes too) when the company has
			# no trade-in account; live's build does not, and csf_tz version-15 no longer runs it.
			("csf_tz.custom_api.create_trade_in_stock_entry", lambda *a, **k: None),
		):
			try:
				p = patch(target, new=new)
				p.start()
				self.addCleanup(p.stop)
			except (ImportError, AttributeError):
				pass

	def _in_step(self, contract):
		"""Order, invoice and contract agree on the money."""
		contract.reload()
		si = _invoice(contract)
		so_paid = flt(frappe.db.get_value("Sales Order", contract.sales_order, "advance_paid"))
		self.assertAlmostEqual(flt(contract.total_paid), _paid(si), places=2)
		self.assertAlmostEqual(flt(contract.total_outstanding), flt(si.outstanding_amount), places=2)
		self.assertAlmostEqual(so_paid, flt(contract.total_paid), places=2)

	# ---------------------------------------------------------------- made by its order

	def test_a_contract_is_made_from_its_sales_order_only(self):
		plot = frappe.db.get_value("Plot Master", {"docstatus": 1}, "name")
		customer = frappe.db.get_value("Customer", {}, "name")
		if not plot or not customer:
			self.skipTest("this site has no plot or customer")
		contract = frappe.get_doc(
			{"doctype": "Plot Contract", "customer": customer, "plot": plot, "contract_date": today()}
		)
		with self.assertRaisesRegex(frappe.ValidationError, "made from its Sales Order"):
			contract.insert(ignore_permissions=True)

	def test_a_handover_delivers_only_from_the_order(self):
		plot = frappe.db.get_value("Plot Master", {"docstatus": 1}, "name")
		customer = frappe.db.get_value("Customer", {}, "name")
		if not plot or not customer:
			self.skipTest("this site has no plot or customer")
		frappe.get_doc(
			{
				"doctype": "Plot Contract",
				"name": "_T-PC7B-NO-ORDER",
				"customer": customer,
				"plot": plot,
				"contract_date": today(),
			}
		).db_insert()
		handover = frappe.new_doc("Plot Handover")
		handover.contract = "_T-PC7B-NO-ORDER"
		handover.plot = plot
		with self.assertRaisesRegex(frappe.ValidationError, "no Sales Order to deliver"):
			handover._ensure_delivery_note()

	# ---------------------------------------------------------------- advance gate

	def test_submit_waits_for_the_first_payment(self):
		contract = _find(
			{"docstatus": 0},
			lambda c: not frappe.db.get_value("Sales Order", c.sales_order, "plot_sales_invoice")
			and not flt(frappe.db.get_value("Sales Order", c.sales_order, "advance_paid")),
		)
		if not contract:
			self.skipTest("no draft contract waiting for its first payment")
		with self.assertRaisesRegex(frappe.ValidationError, "before first payment"):
			contract.submit()

	def test_submit_waits_for_the_advance_then_the_contract_submits_itself(self):
		contract = _find(
			{"docstatus": 0},
			lambda c: (si := _invoice(c)) and 0 < _paid(si) < _advance(c, si),
		)
		if not contract:
			self.skipTest("no draft contract with part of its advance paid")
		si = _invoice(contract)
		with self.assertRaisesRegex(frappe.ValidationError, "advance of TZS .* has not been met"):
			contract.submit()

		_pay(contract, _advance(contract, si) - _paid(si), "_T-PC7B-ADVANCE")

		contract.reload()
		self.assertEqual((contract.docstatus, contract.contract_status), (1, "Ongoing"))
		self.assertEqual(frappe.db.get_value("Plot Master", contract.plot, "status"), "Reserved")
		self._in_step(contract)

	# ---------------------------------------------------------------- completion and reversal

	def test_completion_and_reversal_keep_order_invoice_and_contract_in_step(self):
		contract = _find({"docstatus": 1, "contract_status": "Ongoing"}, _ongoing_with_balance)
		if not contract:
			self.skipTest("no ongoing contract with a balance")
		paid_before = flt(contract.total_paid)
		pe = _pay(contract, flt(_invoice(contract).outstanding_amount), "_T-PC7B-BALANCE")

		contract.reload()
		self.assertEqual((contract.contract_status, contract.payment_progress), ("Completed", "Fully Paid"))
		self.assertEqual(frappe.db.get_value("Plot Master", contract.plot, "status"), "Ready for Handover")
		self.assertEqual(frappe.db.get_value("Journal Entry", contract.government_fee_entry, "docstatus"), 1)
		handover = frappe.db.get_value("Plot Handover", {"contract": contract.name, "docstatus": 0}, "name")
		self.assertTrue(handover)
		self._in_step(contract)

		frappe.get_doc("Payment Entry", pe).cancel()

		contract.reload()
		self.assertEqual(contract.contract_status, "Ongoing")
		self.assertAlmostEqual(flt(contract.total_paid), paid_before, places=2)
		self._in_step(contract)
		# the handover draft left behind cannot hand over a plot that is no longer paid for
		draft = frappe.get_doc("Plot Handover", handover)
		draft.handed_over_by = draft.handed_over_by or "Test"
		draft.received_by = draft.received_by or "Test"
		with self.assertRaisesRegex(frappe.ValidationError, "is not Completed"):
			draft.submit()

	# ---------------------------------------------------------------- overdue

	def test_installment_status_paid_overdue_pending(self):
		contract = frappe.new_doc("Plot Contract")
		day = date(2026, 11, 7)
		yesterday, tomorrow = str(day - timedelta(days=1)), str(day + timedelta(days=1))
		self.assertEqual(contract._derive_installment_status(yesterday, 100, 0, today_date=day), "Paid")
		self.assertEqual(contract._derive_installment_status(yesterday, 100, 40, today_date=day), "Overdue")
		self.assertEqual(contract._derive_installment_status(tomorrow, 100, 40, today_date=day), "Pending")

	# ---------------------------------------------------------------- terminate

	def test_terminate_refuses_a_draft_a_completed_contract_and_an_empty_reason(self):
		draft = frappe.get_all("Plot Contract", filters={"docstatus": 0}, pluck="name", limit=1)
		done = frappe.get_all(
			"Plot Contract", filters={"contract_status": "Completed"}, pluck="name", limit=1
		)
		ongoing = frappe.get_all(
			"Plot Contract", filters={"contract_status": "Ongoing"}, pluck="name", limit=1
		)
		if not (draft and done and ongoing):
			self.skipTest("needs a draft, a completed and an ongoing contract")
		for name in (draft[0], done[0]):
			with self.assertRaisesRegex(frappe.ValidationError, "Only Ongoing or Overdue"):
				frappe.get_doc("Plot Contract", name).terminate_contract("test")
		with self.assertRaisesRegex(frappe.ValidationError, "reason is required"):
			frappe.get_doc("Plot Contract", ongoing[0]).terminate_contract("  ")

	def test_terminate_changes_nothing_when_the_bank_cannot_be_reached(self):
		contract = _find({"docstatus": 1, "contract_status": "Ongoing"}, _ongoing_with_balance)
		if not contract:
			self.skipTest("no ongoing contract with a balance")
		no_answer = {"ok": False, "message": "no answer"}
		with (
			patch(
				"landms.sales_order_hooks.decline_reference_for_sales_order", new=lambda *a, **k: no_answer
			),
			self.assertRaisesRegex(frappe.ValidationError, "bank could not be reached"),
		):
			contract.terminate_contract("test: customer withdrew")

		contract.reload()
		self.assertEqual(contract.contract_status, "Ongoing")
		self.assertFalse(contract.forfeiture_entry)
		self.assertEqual(frappe.db.get_value("Plot Master", contract.plot, "status"), "Reserved")
		self.assertNotEqual(frappe.db.get_value("Sales Order", contract.sales_order, "status"), "Closed")

	def test_terminate_with_payments_forfeits_closes_the_order_and_frees_the_plot(self):
		contract = _find({"docstatus": 1, "contract_status": "Ongoing"}, _ongoing_with_balance)
		if not contract:
			self.skipTest("no ongoing contract with a balance")
		paid = flt(contract.total_paid)
		forfeit_pct = flt(frappe.db.get_single_value("LandMS Settings", "forfeiture_percentage") or 100)
		income = paid * forfeit_pct / 100 - paid * flt(contract.government_share_percent) / 100
		declined = {"ok": True, "message": "declined"}
		with patch(
			"landms.sales_order_hooks.decline_reference_for_sales_order", new=lambda *a, **k: declined
		):
			contract.terminate_contract("test: customer withdrew")

		contract.reload()
		self.assertEqual(contract.contract_status, "Terminated")
		self.assertEqual(contract.termination_reason, "test: customer withdrew")
		if income > 0:
			je = frappe.get_doc("Journal Entry", contract.forfeiture_entry)
			self.assertEqual(je.docstatus, 1)
			self.assertAlmostEqual(flt(je.total_debit), income, places=2)
			self.assertAlmostEqual(flt(je.lms_refund_amount), paid - paid * forfeit_pct / 100, places=2)
		self.assertEqual(frappe.db.get_value("Sales Order", contract.sales_order, "status"), "Closed")
		self.assertEqual(frappe.db.get_value("Plot Master", contract.plot, "status"), "Available")
		self.assertEqual(flt(_invoice(contract).outstanding_amount), 0)

	# ---------------------------------------------------------------- saving the synced figures

	def test_synced_figures_are_saved_on_a_draft_and_a_submitted_contract(self):
		for docstatus in (0, 1):
			name = frappe.db.get_value("Plot Contract", {"docstatus": docstatus}, "name")
			if not name:
				self.skipTest(f"no contract with docstatus {docstatus}")
			contract = frappe.get_doc("Plot Contract", name)
			contract.total_paid, contract.total_outstanding, contract.overpaid_amount = 123.45, 678.9, 1.5
			contract.payment_progress = "Partially Paid"
			contract._persist_payment_sync_state()
			saved = frappe.db.get_value(
				"Plot Contract",
				name,
				["total_paid", "total_outstanding", "overpaid_amount", "payment_progress"],
				as_dict=True,
			)
			self.assertEqual(
				(flt(saved.total_paid), flt(saved.total_outstanding), saved.payment_progress),
				(123.45, 678.9, "Partially Paid"),
			)
			self.assertEqual(flt(saved.overpaid_amount), 1.5)
