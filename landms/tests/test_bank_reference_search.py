"""The bank reference search finds every record that mentions a reference, on real records, and changes nothing."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms.landms.report.landms_bank_reference_search.landms_bank_reference_search import execute


def _no_commit():
	raise AssertionError("a database commit happened inside the test")


class TestBankReferenceSearch(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=AssertionError("outside call"))
		blocker.start()
		self.addCleanup(blocker.stop)
		self.payment = frappe.db.get_value(
			"Payment Entry",
			{"docstatus": 1, "reference_no": ["is", "set"], "payment_type": "Receive"},
			["name", "reference_no", "party", "paid_amount"],
			as_dict=True,
			order_by="creation desc",
		)
		if not self.payment or len(self.payment.reference_no) < 6:
			self.skipTest("this site has no received payment with a bank reference")

	def found(self, reference):
		return execute({"reference": reference})[1]

	def test_a_payment_is_found_by_its_bank_reference(self):
		rows = [r for r in self.found(self.payment.reference_no) if r["record_type"] == "Payment Entry"]
		self.assertIn(self.payment.name, [r["record"] for r in rows])
		row = next(r for r in rows if r["record"] == self.payment.name)
		self.assertEqual((row["state"], row["amount"]), ("Submitted", self.payment.paid_amount))
		self.assertIn(frappe.get_meta("Payment Entry").get_label("reference_no"), row["found_in"])

	def test_part_of_a_reference_is_enough(self):
		part = self.payment.reference_no[-8:]
		self.assertIn(self.payment.name, [r["record"] for r in self.found(part)])

	def test_a_bank_notice_its_payment_and_its_log_are_all_listed(self):
		notice = frappe.db.get_value(
			"TCB Payment Notification",
			{"transaction_id": ["is", "set"], "payment_entry": ["is", "set"]},
			["name", "transaction_id", "payment_entry"],
			as_dict=True,
			order_by="creation desc",
		)
		if not notice or len(notice.transaction_id) < 6:
			self.skipTest("this site has no bank notice that was booked")
		listed = {(r["record_type"], r["record"]) for r in self.found(notice.transaction_id)}
		self.assertIn(("TCB Payment Notification", notice.name), listed)
		self.assertIn(("Payment Entry", notice.payment_entry), listed)
		for log in frappe.get_all(
			"TCB API Log", filters={"transaction_id": notice.transaction_id}, pluck="name"
		):
			self.assertIn(("TCB API Log", log), listed)
		for journal in frappe.get_all(
			"Journal Entry", filters={"cheque_no": notice.transaction_id}, pluck="name"
		):
			self.assertIn(("Journal Entry", journal), listed)

	def test_a_cancelled_journal_says_so(self):
		# a journal has no status field of its own: only its docstatus tells it was cancelled
		cancelled = frappe.db.get_value(
			"Journal Entry",
			{"docstatus": 2, "cheque_no": ["like", "______%"]},
			["name", "cheque_no"],
			as_dict=True,
		)
		if not cancelled:
			self.skipTest("this site has no cancelled journal with a bank reference")
		row = next(r for r in self.found(cancelled.cheque_no) if r["record"] == cancelled.name)
		self.assertEqual(row["state"], "Cancelled")

	def test_a_note_that_names_it_is_found_and_its_text_is_not_shown(self):
		note = frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Comment",
				"reference_doctype": "Payment Entry",
				"reference_name": self.payment.name,
				"content": f"<b>secret words</b> checked {self.payment.reference_no}",
			}
		).insert(ignore_permissions=True)
		row = next(r for r in self.found(self.payment.reference_no) if r["record"] == note.name)
		self.assertEqual((row["state"], row["about"]), ("Note", f"Payment Entry {self.payment.name}"))
		self.assertNotIn("secret words", str(row))

	def test_a_short_reference_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "at least 6"):
			self.found("12345")
		with self.assertRaises(frappe.ValidationError):
			self.found("   ")

	def test_a_reference_nobody_has_finds_nothing(self):
		self.assertEqual(self.found("NO-SUCH-REFERENCE-7f3e9a"), [])

	def test_only_finance_and_system_managers_may_open_it(self):
		path = frappe.get_app_path(
			"landms", "landms", "report", "landms_bank_reference_search", "landms_bank_reference_search.json"
		)
		roles = {r["role"] for r in frappe.parse_json(open(path).read())["roles"]}
		self.assertEqual(roles, {"System Manager", "Accounts Manager", "Accounts User"})
