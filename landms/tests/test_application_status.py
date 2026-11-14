"""One door for a Plot Application's status, on a real application. Every write is rolled back."""

import os
import re
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms.landms.doctype.plot_application.application_status import (
	ALLOWED_INTO,
	STATUSES,
	set_application_status,
)
from landms.landms.doctype.plot_application.plot_application import PlotApplication

MODULE = "landms.landms.doctype.plot_application.plot_application"
OPEN = ("Submitted", "Paid", "Converted")
# every move the app makes today: (from, to)
TODAYS_MOVES = [
	("Draft", "Submitted"),  # submitted with a fee to pay
	("Draft", "Paid"),  # submitted with no fee
	("Submitted", "Paid"),  # fee recorded
	("Paid", "Converted"),  # advance paid
	*[(status, "Cancelled") for status in OPEN],  # cancelled by hand, with its order or contract
	*[(status, "Expired") for status in OPEN],  # ended by the night run
	("Cancelled", "Paid"),  # order reopened
	("Expired", "Paid"),
]


def _no_commit():
	raise AssertionError("a database commit happened inside the test")


class ApplicationCase(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=AssertionError("outside call"))
		blocker.start()
		self.addCleanup(blocker.stop)
		self.app = frappe.db.get_value("Plot Application", {"docstatus": 1, "plot": ["is", "set"]}, "name")
		if not self.app:
			self.skipTest("this site has no submitted application")

	def put(self, status):
		frappe.db.set_value("Plot Application", self.app, "status", status, update_modified=False)

	def status(self):
		return frappe.db.get_value("Plot Application", self.app, "status")

	def notes(self):
		return frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": "Plot Application",
				"reference_name": self.app,
				"comment_type": "Info",
			},
			pluck="content",
			order_by="creation desc",
		)


class TestTheRulebook(ApplicationCase):
	def test_every_move_the_app_makes_today_is_allowed(self):
		for from_status, to_status in TODAYS_MOVES:
			with self.subTest(move=f"{from_status} -> {to_status}"):
				self.put(from_status)
				self.assertTrue(set_application_status(self.app, to_status))
				self.assertEqual(self.status(), to_status)

	def test_a_move_no_code_makes_is_refused_and_nothing_changes(self):
		for from_status, to_status in (
			("Converted", "Paid"),
			("Cancelled", "Converted"),
			("Draft", "Converted"),
			("Paid", "Submitted"),
			("Paid", "Draft"),
		):
			with self.subTest(move=f"{from_status} -> {to_status}"):
				self.put(from_status)
				with self.assertRaises(frappe.ValidationError):
					set_application_status(self.app, to_status)
				self.assertEqual(self.status(), from_status)
		with self.assertRaises(frappe.ValidationError):
			set_application_status(self.app, "Sold")

	def test_the_callers_condition_still_decides(self):
		self.put("Submitted")
		before = len(self.notes())
		self.assertFalse(set_application_status(self.app, "Converted", "advance paid", only_from=("Paid",)))
		self.assertEqual((self.status(), len(self.notes())), ("Submitted", before))

	def test_a_change_leaves_a_short_line_on_the_application(self):
		self.put("Paid")
		set_application_status(self.app, "Converted", "advance paid")
		self.assertEqual(self.notes()[0], "Converted: advance paid")
		set_application_status(self.app, "Expired")
		self.assertEqual(self.notes()[0], "Expired")

	def test_the_same_status_again_writes_no_line(self):
		self.put("Paid")
		before = len(self.notes())
		self.assertTrue(set_application_status(self.app, "Paid", "again"))
		self.assertEqual(len(self.notes()), before)

	def test_an_application_that_does_not_exist_is_left_alone(self):
		self.assertFalse(set_application_status("NO-SUCH-APPLICATION", "Paid"))
		self.assertFalse(set_application_status(None, "Paid"))

	def test_a_document_passed_in_holds_the_new_status_too(self):
		self.put("Paid")
		doc = frappe.get_doc("Plot Application", self.app)
		set_application_status(doc, "Converted", "advance paid")
		self.assertEqual((doc.status, self.status()), ("Converted", "Converted"))

	def test_a_quiet_change_leaves_the_last_changed_time_alone(self):
		self.put("Cancelled")
		before = frappe.db.get_value("Plot Application", self.app, "modified")
		set_application_status(self.app, "Paid", "order reopened", update_modified=False)
		self.assertEqual(frappe.db.get_value("Plot Application", self.app, "modified"), before)

	def test_the_rulebook_covers_exactly_the_statuses_on_the_form(self):
		on_the_form = frappe.get_meta("Plot Application").get_field("status").options.split("\n")
		self.assertEqual(set(ALLOWED_INTO), set(STATUSES))
		self.assertEqual(set(STATUSES), set(on_the_form))


class TestEveryPath(ApplicationCase):
	def application(self, status, **values):
		self.put(status)
		doc = frappe.get_doc("Plot Application", self.app)
		doc.update(values)
		return doc

	def quiet(self):
		"""Hold back the plot, the order and the project summary: only the application is under test."""
		for target in ("set_plot_status", "PlotApplication.create_sales_order"):
			held = patch(f"{MODULE}.{target}", return_value=False)
			held.start()
			self.addCleanup(held.stop)
		for method in ("_sync_land_acquisition_summary", "_cancel_linked_sales_order_if_safe"):
			held = patch.object(PlotApplication, method)
			held.start()
			self.addCleanup(held.stop)

	def test_submitting_with_no_fee_goes_straight_to_paid(self):
		self.quiet()
		doc = self.application("Draft", application_fee=0)
		doc.on_submit()
		self.assertEqual((doc.status, self.status(), self.notes()[0]), ("Paid", "Paid", "Paid: no fee"))

	def test_submitting_with_a_fee_waits_as_submitted(self):
		self.quiet()
		doc = self.application("Draft", application_fee=5000)
		doc.on_submit()
		self.assertEqual(
			(doc.status, self.status(), self.notes()[0]), ("Submitted", "Submitted", "Submitted")
		)

	def test_recording_the_fee_makes_it_paid(self):
		self.quiet()
		doc = self.application("Submitted", application_fee=5000, sales_invoice="")
		frappe.db.set_value("Plot Master", doc.plot, "status", "Pending Fee", update_modified=False)
		settings = frappe._dict(
			company="A Company",
			application_fee_receiving_account="A Bank Account",
			application_fee_item="A Fee Item",
			application_fee_income_account="An Income Account",
		)
		real_get_doc = frappe.get_doc

		def fee_documents_only(*args, **kwargs):
			made = args[0] if args and isinstance(args[0], dict) else {}
			if made.get("doctype") in ("Sales Invoice", "Payment Entry"):
				fee_document = MagicMock(debit_to="A Receivable Account")
				fee_document.name = "TEST " + made["doctype"]
				return fee_document
			return real_get_doc(*args, **kwargs)

		with (
			patch(f"{MODULE}.frappe.get_single", return_value=settings),
			patch(f"{MODULE}.frappe.get_doc", side_effect=fee_documents_only),
			patch.object(PlotApplication, "_lock_plot_row"),
			patch.object(PlotApplication, "_get_other_active_application", return_value=None),
			patch.object(PlotApplication, "_validate_receiving_account"),
		):
			doc.record_fee_payment(frappe.utils.today())
		self.assertEqual((doc.status, self.status(), self.notes()[0]), ("Paid", "Paid", "Paid: fee recorded"))

	def test_cancelling_ends_it_as_cancelled_or_expired(self):
		self.quiet()
		for flags, status, line in (
			({}, "Cancelled", "Cancelled"),
			({"_cancellation_reason": "Expired"}, "Expired", "Expired"),
			({"_cancellation_reason": "Contract Terminated"}, "Cancelled", "Cancelled: contract terminated"),
			({"from_sales_order_cancel": True}, "Cancelled", "Cancelled: order cancelled"),
		):
			with self.subTest(flags=flags):
				doc = self.application("Paid")
				doc.flags.update(flags)
				doc.on_cancel()
				self.assertEqual((doc.status, self.status(), self.notes()[0]), (status, status, line))

	def test_the_advance_converts_only_a_paid_application(self):
		self.put("Paid")
		self.assertTrue(set_application_status(self.app, "Converted", "advance paid", only_from=("Paid",)))
		self.assertEqual(self.notes()[0], "Converted: advance paid")
		self.assertFalse(set_application_status(self.app, "Converted", "advance paid", only_from=("Paid",)))

	def test_reopening_its_order_makes_it_paid_again(self):
		for ended in ("Cancelled", "Expired"):
			with self.subTest(ended=ended):
				self.put(ended)
				set_application_status(self.app, "Paid", "order reopened", update_modified=False)
				self.assertEqual((self.status(), self.notes()[0]), ("Paid", "Paid: order reopened"))


class TestOneDoor(FrappeTestCase):
	def test_only_the_one_door_writes_an_applications_status(self):
		app = os.path.dirname(frappe.get_app_path("landms"))
		door = os.path.join("plot_application", "application_status.py")
		by_name = re.compile(r"""set_value\(\s*["']Plot Application["'][^)]*["']status["']""", re.S)
		by_document = re.compile(r"""db_set\(\s*["']status["']""")
		offenders = []
		for root, _dirs, files in os.walk(os.path.join(app, "landms")):
			if "tests" in root.split(os.sep):
				continue
			for name in files:
				path = os.path.join(root, name)
				if not name.endswith(".py") or path.endswith(door):
					continue
				source = open(path, encoding="utf-8").read()
				in_the_form = path.endswith(os.path.join("plot_application", "plot_application.py"))
				if by_name.search(source) or (in_the_form and by_document.search(source)):
					offenders.append(os.path.relpath(path, app))
		self.assertEqual(offenders, [], "these files write an application's status themselves")
