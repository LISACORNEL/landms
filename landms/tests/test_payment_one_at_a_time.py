"""Payments on one order are recorded one at a time, whichever rail they come from.

Two payments landing together used to read the same balance, and the invoice
then lost one of them (proven on a local site: 4 runs out of 4). Now each
payment takes the order's lock, and one that read figures another payment has
since changed is refused, unsaved. The rails (TCB notice and reconciliation,
Malipopay booking) then wait and record it again with fresh figures — TCB never
resends a notice (proven 14 Oct 2026: the second of two simultaneous notices was
refused before this; with it both are recorded and the invoice matches the
ledger, ~/Desktop/plot-audit/race_proof_tcb_notice.py).
"""

import threading
import time
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import payment_sync


def _no_commit():
	raise AssertionError("a database commit happened inside the test")


def order_with_plot_invoice():
	"""A submitted order, its submitted plot invoice and what is still owed on it."""
	so, si, item = (frappe.qb.DocType(d) for d in ("Sales Order", "Sales Invoice", "Sales Invoice Item"))
	rows = (
		frappe.qb.from_(so)
		.inner_join(si)
		.on(si.name == so.plot_sales_invoice)
		.inner_join(item)
		.on((item.parent == si.name) & (item.sales_order == so.name))
		.select(so.name, so.plot_sales_invoice, si.outstanding_amount)
		.where(so.docstatus == 1)
		.where(si.docstatus == 1)
		.where(si.is_plot_sale_invoice == 1)
		.limit(1)
		.run()
	)
	return rows[0] if rows else None


def on_another_connection(job):
	"""Run a job the way a second payment arrives: on its own database connection."""
	site, sites_path, box = frappe.local.site, frappe.local.sites_path, {}

	def run():
		frappe.init(site=site, sites_path=sites_path)
		frappe.connect()
		started = time.monotonic()
		try:
			box["result"] = job()
		except Exception as e:
			box["error"] = e
		finally:
			box["seconds"] = time.monotonic() - started
			frappe.db.rollback()
			frappe.destroy()

	thread = threading.Thread(target=run)
	thread.start()
	return thread, box


class TestPaymentOneAtATime(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		row = order_with_plot_invoice()
		if not row:
			self.skipTest("this site has no Sales Order with a plot invoice")
		self.so, self.si, self.outstanding = row

	def take_at_once(self):
		return frappe.db.get_value("Sales Order", self.so, "name", for_update=True, wait=False)

	def test_unchanged_balance_is_accepted(self):
		payment_sync._refuse_if_read_before_another_payment(
			"Sales Invoice", self.si, "outstanding_amount", self.outstanding
		)

	def test_balance_changed_by_another_payment_is_refused(self):
		with self.assertRaises(payment_sync.ConcurrentPaymentError):
			payment_sync._refuse_if_read_before_another_payment(
				"Sales Invoice", self.si, "outstanding_amount", self.outstanding + 30
			)

	def test_invoice_created_by_another_first_payment_is_refused(self):
		# a second "first payment" must not build a second plot invoice
		with self.assertRaises(payment_sync.ConcurrentPaymentError):
			payment_sync._refuse_if_read_before_another_payment(
				"Sales Order", self.so, "plot_sales_invoice", None
			)

	def test_the_order_is_held_until_the_transaction_ends(self):
		payment_sync._lock_sales_order_for_payment(self.so)
		thread, second = on_another_connection(self.take_at_once)
		thread.join()
		self.assertIsInstance(second.get("error"), frappe.QueryTimeoutError)

		frappe.db.rollback()  # a commit lets go the same way
		thread, second = on_another_connection(self.take_at_once)
		thread.join()
		self.assertEqual(second.get("result"), self.so)

	def test_a_second_payment_waits_for_the_first_to_finish(self):
		payment_sync._lock_sales_order_for_payment(self.so)
		thread, second = on_another_connection(lambda: payment_sync._lock_sales_order_for_payment(self.so))
		time.sleep(0.6)
		self.assertTrue(thread.is_alive())  # still waiting
		frappe.db.rollback()
		thread.join(timeout=10)
		self.assertNotIn("error", second)
		self.assertGreaterEqual(second["seconds"], 0.5)

	def test_a_second_payment_is_refused_unsaved_after_the_wait(self):
		payment_sync._lock_sales_order_for_payment(self.so)
		with patch.object(payment_sync, "PAYMENT_LOCK_WAIT_SECONDS", 0.5):
			thread, second = on_another_connection(
				lambda: payment_sync._lock_sales_order_for_payment(self.so)
			)
			thread.join(timeout=10)
		self.assertIsInstance(second.get("error"), payment_sync.ConcurrentPaymentError)
		self.assertGreaterEqual(second["seconds"], 0.5)

	def test_the_balance_check_holds_the_invoice_too(self):
		# a plain read would show this payment's old picture; the check must read the row under lock
		payment_sync._refuse_if_read_before_another_payment(
			"Sales Invoice", self.si, "outstanding_amount", self.outstanding
		)
		thread, second = on_another_connection(
			lambda: frappe.db.get_value("Sales Invoice", self.si, "name", for_update=True, wait=False)
		)
		thread.join()
		self.assertIsInstance(second.get("error"), frappe.QueryTimeoutError)

	def test_the_same_payment_can_take_its_own_order_twice(self):
		payment_sync._lock_sales_order_for_payment(self.so)
		payment_sync._lock_sales_order_for_payment(self.so)


class TestPaymentLookups(FrappeTestCase):
	"""The look-ups a payment makes, on a real order; the shortcut is blanked to reach the full search."""

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		row = order_with_plot_invoice()
		if not row:
			self.skipTest("this site has no Sales Order with a plot invoice")
		self.so, self.si, _ = row

	def blank_the_shortcut(self):
		frappe.db.set_value("Sales Order", self.so, "plot_sales_invoice", None, update_modified=False)

	def test_an_order_finds_its_plot_invoice(self):
		self.assertEqual(payment_sync._get_plot_invoice_name_from_sales_order(self.so), self.si)
		self.blank_the_shortcut()
		found = payment_sync._get_plot_invoice_name_from_sales_order(self.so)
		self.assertTrue(frappe.db.exists("Sales Invoice Item", {"parent": found, "sales_order": self.so}))
		self.assertEqual(frappe.db.get_value("Sales Invoice", found, "is_plot_sale_invoice"), 1)

	def test_a_plot_invoice_finds_its_order(self):
		self.assertEqual(payment_sync._get_sales_order_name_from_plot_invoice(self.si), self.so)
		self.blank_the_shortcut()
		self.assertEqual(payment_sync._get_sales_order_name_from_plot_invoice(self.si), self.so)

	def test_an_unknown_order_or_invoice_finds_nothing(self):
		self.assertEqual(payment_sync._get_plot_invoice_name_from_sales_order("NO-SUCH-ORDER"), "")
		self.assertEqual(payment_sync._get_sales_order_name_from_plot_invoice("NO-SUCH-INVOICE"), "")

	def test_only_a_submitted_payment_counts_as_an_already_booked_reference(self):
		pe, ref = frappe.qb.DocType("Payment Entry"), frappe.qb.DocType("Payment Entry Reference")
		payments = (
			frappe.qb.from_(pe)
			.inner_join(ref)
			.on(ref.parent == pe.name)
			.select(pe.name, pe.reference_no, ref.reference_name)
			.where(pe.docstatus == 1)
			.where(ref.reference_doctype == "Sales Invoice")
			.where(pe.reference_no.isnotnull())
			.where(pe.reference_no != "")
			.limit(1)
			.run()
		)
		if not payments:
			self.skipTest("this site has no submitted payment on an invoice")
		payment, reference, invoice = payments[0]
		self.assertTrue(payment_sync._payment_reference_exists(invoice, reference))
		self.assertFalse(payment_sync._payment_reference_exists(invoice, "NO-SUCH-REFERENCE"))
		self.assertFalse(payment_sync._payment_reference_exists("NO-SUCH-INVOICE", reference))

		# once that payment is cancelled, its reference is free again
		others = frappe.get_all(
			"Payment Entry", filters={"reference_no": reference, "docstatus": 1, "name": ["!=", payment]}
		)
		if not others:
			frappe.db.set_value("Payment Entry", payment, "docstatus", 2, update_modified=False)
			self.assertFalse(payment_sync._payment_reference_exists(invoice, reference))


class TestRailsWaitForOtherPayments(FrappeTestCase):
	"""record_payment_waiting_for_others: the rails' way in."""

	def setUp(self):
		self.commits = 0

		def count_commit(*args, **kwargs):
			self.commits += 1

		p = patch.object(frappe.db, "commit", count_commit)
		p.start()
		self.addCleanup(p.stop)

	def test_a_clash_waits_and_records_with_fresh_figures(self):
		answers = [payment_sync.ConcurrentPaymentError("clash"), "PE-TEST-1"]

		def recorder(**kwargs):
			answer = answers.pop(0)
			if isinstance(answer, Exception):
				raise answer
			return answer

		with patch("landms.payment_sync.create_payment_entry_for_sales_order", side_effect=recorder):
			self.assertEqual(
				payment_sync.record_payment_waiting_for_others(sales_order_name="SO-T"), "PE-TEST-1"
			)
		self.assertEqual(self.commits, 1)  # one fresh start between the attempts

	def test_it_gives_up_after_repeated_clashes(self):
		with (
			patch(
				"landms.payment_sync.create_payment_entry_for_sales_order",
				side_effect=payment_sync.ConcurrentPaymentError("clash"),
			) as recorder,
			self.assertRaises(payment_sync.ConcurrentPaymentError),
		):
			payment_sync.record_payment_waiting_for_others(sales_order_name="SO-T")
		self.assertEqual(recorder.call_count, payment_sync.CONCURRENT_ATTEMPTS)

	def test_other_errors_are_not_retried(self):
		with (
			patch(
				"landms.payment_sync.create_payment_entry_for_sales_order",
				side_effect=frappe.ValidationError("no outstanding balance"),
			) as recorder,
			self.assertRaises(frappe.ValidationError),
		):
			payment_sync.record_payment_waiting_for_others(sales_order_name="SO-T")
		self.assertEqual(recorder.call_count, 1)
		self.assertEqual(self.commits, 0)

	def test_tcb_and_malipopay_use_the_waiting_recorder(self):
		import inspect

		from landms import malipopay, tcb

		self.assertIn(
			"record_payment_waiting_for_others", inspect.getsource(tcb.apply_tcb_payment_to_sales_order)
		)
		self.assertIn("record_payment_waiting_for_others", inspect.getsource(malipopay._book_one_receipt))
