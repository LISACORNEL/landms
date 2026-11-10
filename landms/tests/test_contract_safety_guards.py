"""Safety guards on a plot sale's money (PC-12). Real records, every write rolled back."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, today

from landms.payment_sync import before_submit_unreconcile_payment, create_payment_entry_for_sales_order
from landms.receipt_verification import judge, money_owed


def _no_commit():
	raise AssertionError("a database commit happened inside the PC-12 test")


def _no_outside_call(*args, **kwargs):
	raise AssertionError("an outside call was attempted inside the PC-12 test")


def _invoice(contract):
	name = frappe.db.get_value("Sales Order", contract.sales_order, "plot_sales_invoice")
	return frappe.get_doc("Sales Invoice", name) if name else None


def _ongoing_with_balance(wanted=None):
	for name in frappe.get_all(
		"Plot Contract",
		filters={"docstatus": 1, "contract_status": "Ongoing"},
		pluck="name",
		order_by="name desc",
	):
		contract = frappe.get_doc("Plot Contract", name)
		so = frappe.db.get_value("Sales Order", contract.sales_order, ["docstatus", "status"], as_dict=True)
		si = _invoice(contract)
		if (
			so
			and so.docstatus == 1
			and so.status not in ("Closed", "Cancelled", "Completed")
			and si
			and si.docstatus == 1
			and flt(si.outstanding_amount) > 0
			and frappe.db.get_value("Plot Master", contract.plot, "status") == "Reserved"
			and not frappe.db.exists("Plot Handover", {"contract": name, "docstatus": ["!=", 2]})
			and (wanted is None or wanted(contract, si))
		):
			return contract
	return None


def _payments(contract):
	si = _invoice(contract)
	return frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": si.name, "docstatus": 1},
		pluck="parent",
		order_by="creation",
	)


def _pay(contract, amount, ref):
	return create_payment_entry_for_sales_order(
		sales_order_name=contract.sales_order,
		amount=amount,
		payment_date=None,
		bank_account=frappe.db.get_single_value("LandMS Settings", "tcb_bank_account"),
		reference_no=ref,
	)


def _terminate(contract):
	declined = {"ok": True, "message": "declined"}
	with patch("landms.sales_order_hooks.decline_reference_for_sales_order", new=lambda *a, **k: declined):
		frappe.get_doc("Plot Contract", contract.name).terminate_contract("test: customer withdrew")


class TestContractSafetyGuards(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		for target, new in (
			("requests.sessions.Session.request", _no_outside_call),
			# the bench's csf_tz stops every new invoice; live's build does not
			("csf_tz.custom_api.create_trade_in_stock_entry", lambda *a, **k: None),
		):
			try:
				p = patch(target, new=new)
				p.start()
				self.addCleanup(p.stop)
			except (ImportError, AttributeError):
				pass

	def _contract(self, wanted=None):
		contract = _ongoing_with_balance(wanted)
		if not contract:
			self.skipTest("no suitable ongoing contract on this site")
		return contract

	# ---------------------------------------------------------------- terminated contracts

	def test_the_money_of_a_terminated_contract_never_moves(self):
		contract = self._contract()
		payment = _payments(contract)[0]
		_terminate(contract)
		si = _invoice(contract)

		with self.assertRaisesRegex(frappe.ValidationError, "is terminated"):
			frappe.get_doc("Payment Entry", payment).cancel()
		with self.assertRaisesRegex(frappe.ValidationError, "is terminated"):
			before_submit_unreconcile_payment(
				frappe._dict(
					allocations=[frappe._dict(reference_doctype="Sales Invoice", reference_name=si.name)]
				)
			)

	def test_a_terminated_contract_never_changes_again(self):
		contract = self._contract()
		_terminate(contract)
		contract.reload()
		before = (contract.contract_status, flt(contract.total_paid), flt(contract.total_outstanding))
		# money on its invoice changes some other way: the contract stays as it was
		frappe.db.set_value("Sales Invoice", _invoice(contract).name, "outstanding_amount", 1000)
		frappe.get_doc("Plot Contract", contract.name).sync_payment_status()

		contract.reload()
		self.assertEqual(
			(contract.contract_status, flt(contract.total_paid), flt(contract.total_outstanding)), before
		)

	# ---------------------------------------------------------------- handover

	def test_a_payment_under_a_handed_over_plot_cannot_be_cancelled(self):
		contract = self._contract()
		pe = _pay(contract, flt(_invoice(contract).outstanding_amount), "_T-PC12-BALANCE")
		handover = frappe.get_doc("Plot Handover", {"contract": contract.name, "docstatus": 0})
		handover.handed_over_by, handover.received_by, handover.handover_date = "Test", "Test", today()
		# the handover saves its delivery note mid-step; keep that inside this rolled-back test
		with patch.object(frappe.db, "commit", new=lambda *a, **k: None):
			handover.submit()

		with self.assertRaisesRegex(frappe.ValidationError, "Cancel the handover first"):
			frappe.get_doc("Payment Entry", pe).cancel()

	def test_a_completed_contract_that_owes_money_is_no_longer_completed(self):
		# the advance came in one payment and the balance is smaller than the advance
		contract = self._contract(
			lambda c, si: len(_payments(c)) == 1
			and flt(si.outstanding_amount) < flt(si.grand_total) * flt(c.booking_fee_percent) / 100
		)
		advance_payment = _payments(contract)[0]
		_pay(contract, flt(_invoice(contract).outstanding_amount), "_T-PC12-BALANCE")
		contract.reload()
		self.assertEqual(contract.contract_status, "Completed")

		# the advance is cancelled: one step takes the money below the advance
		frappe.get_doc("Payment Entry", advance_payment).cancel()

		contract.reload()
		self.assertGreater(flt(_invoice(contract).outstanding_amount), 0)
		self.assertNotEqual(contract.contract_status, "Completed")

	def test_the_handover_waits_until_nothing_is_owed(self):
		contract = self._contract()
		_pay(contract, flt(_invoice(contract).outstanding_amount), "_T-PC12-BALANCE")
		frappe.get_doc("Payment Entry", _payments(contract)[0]).cancel()
		# even if the contract still said Completed, money owed stops the handover
		frappe.db.set_value("Plot Contract", contract.name, "contract_status", "Completed")
		handover = frappe.get_doc("Plot Handover", {"contract": contract.name, "docstatus": 0})
		handover.handed_over_by, handover.received_by, handover.handover_date = "Test", "Test", today()

		with self.assertRaisesRegex(frappe.ValidationError, "not fully paid"):
			handover.submit()

	def test_the_receipt_is_valid_only_when_nothing_is_owed(self):
		handover = frappe._dict(docstatus=1)
		self.assertEqual(judge(handover, "Completed", 0)[0], "valid")
		self.assertEqual(judge(handover, "Completed", 100)[0], "cancelled")
		self.assertEqual(judge(handover, "Completed", None)[0], "cancelled")
		contract = self._contract()
		self.assertEqual(money_owed(contract.name), flt(_invoice(contract).outstanding_amount))

	# ---------------------------------------------------------------- failures

	def test_a_failed_contract_update_stops_the_payment_cancel(self):
		contract = self._contract()
		pe = _pay(contract, 1000, "_T-PC12-SMALL")
		with (
			patch(
				"landms.payment_sync._sync_contract_after_payment", side_effect=RuntimeError("update failed")
			),
			self.assertRaises(RuntimeError),
		):
			frappe.get_doc("Payment Entry", pe).cancel()

	def test_the_guards_are_wired_on_erpnext_doctypes(self):
		events = frappe.get_module("landms.hooks").doc_events
		self.assertEqual(
			events["Payment Entry"]["before_cancel"], "landms.payment_sync.before_cancel_payment_entry"
		)
		self.assertEqual(
			events["Unreconcile Payment"]["before_submit"],
			"landms.payment_sync.before_submit_unreconcile_payment",
		)
