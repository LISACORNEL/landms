"""Receipt verification — the TRA-style QR on the Plot Handover receipt.

Covers the code format, the live judgement (valid / cancelled / not_found),
what the public check returns, the print-format QR and the backfill patch.
Lookups for the check itself are faked, the check's log is captured instead of
written, and everything else rolls back.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import receipt_verification as rv

CODE = "ABCD2345EFGH"


class TestReceiptCode(FrappeTestCase):
	def test_normalize_accepts_what_people_type(self):
		self.assertEqual(rv.normalize_code("abcd-2345-efgh"), CODE)
		self.assertEqual(rv.normalize_code(" ABCD 2345 EFGH "), CODE)
		# O, I and L are read as 0, 1 and 1
		self.assertEqual(rv.normalize_code("O1L1-0000-0000"), "011100000000")

	def test_normalize_refuses_what_cannot_be_a_code(self):
		self.assertEqual(rv.normalize_code(""), "")
		self.assertEqual(rv.normalize_code("ABCD2345EFG"), "")  # too short
		self.assertEqual(rv.normalize_code("ABCD2345EFGU"), "")  # U is not in the alphabet
		self.assertEqual(rv.normalize_code("PHO-.26.08.-.0006"), "")  # a receipt number is not a code

	def test_display_groups_in_fours(self):
		self.assertEqual(rv.display_code(CODE), "ABCD-2345-EFGH")

	def test_new_code_is_random_crockford(self):
		codes = {rv.new_code() for _i in range(20)}
		self.assertEqual(len(codes), 20)
		for code in codes:
			self.assertEqual(len(code), rv.CODE_LENGTH)
			self.assertTrue(all(ch in rv.CODE_ALPHABET for ch in code))

	def test_every_submit_gets_a_fresh_code(self):
		# go-live list 24/25: a copied or typed-in code never reaches a receipt
		doc = frappe._dict(verification_code=None)
		rv.assign_code(doc)
		self.assertEqual(len(doc.verification_code), rv.CODE_LENGTH)
		doc = frappe._dict(verification_code=CODE)
		rv.assign_code(doc)
		self.assertNotEqual(doc.verification_code, CODE)
		self.assertEqual(len(doc.verification_code), rv.CODE_LENGTH)

	def test_qr_address_comes_from_landms_settings(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.set_single_value(
			"LandMS Settings", "receipt_verification_address", "https://verify.gva.co.tz/"
		)
		self.assertEqual(rv.verify_url(CODE), "https://verify.gva.co.tz/verify/" + CODE)
		frappe.db.set_single_value("LandMS Settings", "receipt_verification_address", "https://example.test")
		self.assertEqual(rv.verify_url(CODE), "https://example.test/verify/" + CODE)


class TestReceiptJudgement(FrappeTestCase):
	def test_unknown_or_draft_is_not_found(self):
		self.assertEqual(rv.judge(None, None)[0], "not_found")
		self.assertEqual(rv.judge(frappe._dict(docstatus=0), "Completed")[0], "not_found")

	def test_cancelled_handover_is_cancelled(self):
		self.assertEqual(rv.judge(frappe._dict(docstatus=2), "Completed")[0], "cancelled")

	def test_contract_no_longer_completed_is_cancelled(self):
		for status in ("Ongoing", "Terminated", None):
			self.assertEqual(rv.judge(frappe._dict(docstatus=1), status)[0], "cancelled")

	def test_submitted_handover_on_completed_contract_is_valid(self):
		self.assertEqual(rv.judge(frappe._dict(docstatus=1), "Completed"), ("valid", "valid"))


class TestVerifyReceipt(FrappeTestCase):
	"""The public check. Called without its rate limiter (that is Frappe's own
	tested code) so no counters are touched."""

	def setUp(self):
		self.handover = None
		self.contract_status = "Completed"
		self.owed = 0
		self.logged = []
		real_get_value = frappe.db.get_value

		def get_value(doctype, filters=None, fieldname="name", *args, **kwargs):
			if doctype == "Plot Handover" and isinstance(filters, dict) and "verification_code" in filters:
				return self.handover if self.handover and filters["verification_code"] == CODE else None
			if doctype == "Plot Contract" and fieldname == "contract_status":
				return self.contract_status
			if doctype == "Customer" and fieldname == "customer_name":
				return "TEST HOLDER"
			if doctype == "Plot Master" and fieldname == "plot_number":
				return "PLOT NO 01 BLOCK 1A"
			return real_get_value(doctype, filters, fieldname, *args, **kwargs)

		for p in (
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch("landms.receipt_verification._log", side_effect=lambda *a: self.logged.append(a)),
			patch("landms.receipt_verification.money_owed", side_effect=lambda contract: self.owed),
		):
			p.start()
			self.addCleanup(p.stop)
		self.check = rv.verify_receipt.__wrapped__

	def _submitted(self, docstatus=1):
		self.handover = frappe._dict(
			name="PHO-TEST-0001",
			docstatus=docstatus,
			contract="CTR-TEST-0001",
			customer="CUST-TEST",
			plot="PLT-TEST",
			acquisition_name="TEST PROJECT",
			selling_price=9685700,
			handover_date="2026-10-13",
		)

	def test_valid_receipt_returns_its_details(self):
		self._submitted()
		out = self.check(code="abcd-2345-efgh")
		self.assertTrue(out["valid"])
		self.assertEqual(out["status"], "valid")
		receipt = out["receipt"]
		self.assertEqual(receipt["receipt_no"], "PHO-TEST-0001")
		self.assertEqual(receipt["holder"], "TEST HOLDER")
		self.assertEqual(receipt["plot"], "PLOT NO 01 BLOCK 1A")
		self.assertEqual(receipt["code"], "ABCD-2345-EFGH")
		self.assertIn("9,685,700", receipt["amount_paid"])
		self.assertEqual(self.logged[-1][2], "valid")

	def test_a_receipt_with_money_owed_is_cancelled(self):
		self._submitted()
		for owed in (100, None):
			self.owed = owed
			self.assertEqual(self.check(code="abcd-2345-efgh"), {"valid": False, "status": "cancelled"})

	def test_unknown_code_says_not_found_and_nothing_else(self):
		out = self.check(code="ZZZZ-ZZZZ-ZZZZ")
		self.assertEqual(out, {"valid": False, "status": "not_found"})

	def test_garbage_says_not_found(self):
		self.assertEqual(self.check(code="<script>"), {"valid": False, "status": "not_found"})
		self.assertEqual(self.check(code=None), {"valid": False, "status": "not_found"})

	def test_cancelled_handover_shows_no_details(self):
		self._submitted(docstatus=2)
		self.assertEqual(self.check(code=CODE), {"valid": False, "status": "cancelled"})

	def test_reversed_payment_stops_the_receipt_verifying(self):
		self._submitted()
		self.contract_status = "Ongoing"
		self.assertEqual(self.check(code=CODE), {"valid": False, "status": "cancelled"})

	def test_every_check_is_logged(self):
		self.check(code="ZZZZ-ZZZZ-ZZZZ")
		self._submitted()
		self.check(code=CODE)
		self.assertEqual([entry[2] for entry in self.logged], ["not_found", "valid"])


class TestReceiptPrint(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.set_single_value(
			"LandMS Settings", "receipt_verification_address", "https://verify.gva.co.tz"
		)

	def test_no_qr_until_the_verification_address_is_set(self):
		# operator, 15 Oct 2026: no receipt may carry an address that does not open the page yet
		frappe.db.set_single_value("LandMS Settings", "receipt_verification_address", "")
		self.assertEqual(
			rv.landms_receipt_verification(frappe._dict(docstatus=1, verification_code=CODE)), {}
		)

	def test_draft_or_codeless_receipt_prints_no_qr(self):
		self.assertEqual(
			rv.landms_receipt_verification(frappe._dict(docstatus=0, verification_code=CODE)), {}
		)
		self.assertEqual(
			rv.landms_receipt_verification(frappe._dict(docstatus=1, verification_code=None)), {}
		)

	def test_submitted_receipt_prints_a_qr_of_the_gva_address(self):
		out = rv.landms_receipt_verification(frappe._dict(docstatus=1, verification_code=CODE))
		self.assertEqual(out["url"], "https://verify.gva.co.tz/verify/" + CODE)
		self.assertEqual(out["display"], "ABCD-2345-EFGH")
		self.assertTrue(out["qr"].startswith("data:image/png;base64,"))


class TestBackfillPatch(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_backfill_codes_only_submitted_handovers(self):
		from landms.patches import backfill_receipt_verification_codes as patch_module

		drafts_before = frappe.db.count("Plot Handover", {"docstatus": 0, "verification_code": ["is", "set"]})
		with patch.object(frappe.db, "commit", lambda *a, **k: None):
			patch_module.execute()
		self.assertEqual(
			frappe.db.count("Plot Handover", {"docstatus": 1, "verification_code": ["is", "not set"]}), 0
		)
		self.assertEqual(
			frappe.db.count("Plot Handover", {"docstatus": 0, "verification_code": ["is", "set"]}),
			drafts_before,
		)


class TestHandoverIsNeverBlocked(FrappeTestCase):
	"""Operator, 15 Oct 2026: the QR is an extra; issuing a handover must never
	depend on it."""

	def test_no_code_can_be_made_and_the_handover_still_submits(self):
		doc = frappe._dict(verification_code=None)
		with (
			patch("landms.receipt_verification.new_code", side_effect=frappe.ValidationError("no code")),
			patch("frappe.log_error") as logged,
		):
			rv.assign_code(doc)  # must not raise
		self.assertIsNone(doc.verification_code)
		logged.assert_called_once()

	def test_a_receipt_without_a_code_prints_without_a_qr(self):
		self.assertEqual(
			rv.landms_receipt_verification(frappe._dict(docstatus=1, verification_code=None)), {}
		)

	def test_a_broken_qr_never_stops_the_print(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.set_single_value(
			"LandMS Settings", "receipt_verification_address", "https://verify.gva.co.tz"
		)
		with patch("pyqrcode.create", side_effect=RuntimeError("png broken")), patch("frappe.log_error"):
			self.assertEqual(
				rv.landms_receipt_verification(frappe._dict(docstatus=1, verification_code=CODE)), {}
			)


class TestAmendedReceipt(FrappeTestCase):
	"""Live test, 15 Oct 2026: Amend copied the cancelled receipt's code and the
	amended handover could not be saved (Duplicate entry for verification_code)."""

	def test_a_draft_never_carries_a_code(self):
		from landms.landms.doctype.plot_handover.plot_handover import PlotHandover

		doc = frappe.get_doc({"doctype": "Plot Handover", "verification_code": CODE})
		with (
			patch.object(PlotHandover, "_fill_from_contract"),
			patch.object(PlotHandover, "_fill_company_representative"),
		):
			doc.run_method("before_validate")
		self.assertIsNone(doc.verification_code)
