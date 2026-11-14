"""A forfeiture journal points back at its order. Real records, every write rolled back."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import sales_order_hooks
from landms.patches.backfill_forfeiture_journal_order import ORDER_IN_REMARK, REMARK, backfill

MODULE = "landms.sales_order_hooks"


def _no_commit():
	raise AssertionError("a database commit happened inside the test")


class ForfeitureCase(FrappeTestCase):
	def setUp(self):
		if not frappe.get_meta("Journal Entry").has_field("lms_sales_order"):
			self.skipTest("migrate this site first: Journal Entry has no order link yet")
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=AssertionError("outside call"))
		blocker.start()
		self.addCleanup(blocker.stop)


class TestTheLink(ForfeitureCase):
	def test_the_field_is_a_read_only_link_to_the_order(self):
		field = frappe.get_meta("Journal Entry").get_field("lms_sales_order")
		self.assertEqual(
			(field.fieldtype, field.options, field.read_only, field.no_copy), ("Link", "Sales Order", 1, 1)
		)

	def test_a_new_forfeiture_journal_carries_its_order(self):
		real_get_doc = frappe.get_doc
		built = {}

		def journal_only(*args, **kwargs):
			made = args[0] if args and isinstance(args[0], dict) else {}
			if made.get("doctype") != "Journal Entry":
				return real_get_doc(*args, **kwargs)
			built.update(made)
			journal = MagicMock()
			journal.name = "TEST-JOURNAL-1"
			return journal

		# a real order with money paid and its contract still a draft: what a close forfeits
		paid_drafts = frappe.get_all(
			"Plot Contract", filters={"docstatus": 0, "total_paid": [">", 0]}, pluck="sales_order", limit=20
		)
		for name in paid_drafts:
			order = frappe.get_doc("Sales Order", name)
			if order.docstatus != 1 or order.get("forfeiture_entry"):
				continue
			with (
				patch(f"{MODULE}.frappe.get_doc", side_effect=journal_only),
				patch.object(order, "db_set") as pointer,
			):
				sales_order_hooks._post_draft_contract_forfeiture_je(order)
			if built:
				break
		if not built:
			self.skipTest("this site has no paid order whose contract is still a draft")
		self.assertEqual(built["lms_sales_order"], order.name)
		# the backfill finds older journals by this remark: the two must keep agreeing
		self.assertEqual(ORDER_IN_REMARK.match(built["user_remark"]).group(1), order.name)
		pointer.assert_called_once_with("forfeiture_entry", "TEST-JOURNAL-1")


class TestTheBackfill(ForfeitureCase):
	def setUp(self):
		super().setUp()
		self.journals = frappe.get_all(
			"Journal Entry", filters={"user_remark": ["like", REMARK]}, fields=["name", "user_remark"]
		)
		if not self.journals:
			self.skipTest("this site has no forfeiture journal")
		journal = frappe.qb.DocType("Journal Entry")
		# the state before the backfill: no journal knows its order
		frappe.qb.update(journal).set(journal.lms_sales_order, None).run()

	def order_of(self, journal):
		return frappe.db.get_value("Journal Entry", journal, "lms_sales_order")

	def named_in_remark(self, row):
		return ORDER_IN_REMARK.match(row.user_remark).group(1)

	def one_with_a_living_order(self):
		for row in self.journals:
			if frappe.db.exists("Sales Order", self.named_in_remark(row)):
				return row
		self.skipTest("no forfeiture journal names an order that still exists")

	def test_every_journal_is_linked_to_the_order_its_remark_names(self):
		gone = [
			row for row in self.journals if not frappe.db.exists("Sales Order", self.named_in_remark(row))
		]
		filled, skipped = backfill()
		self.assertEqual((filled, skipped), (len(self.journals) - len(gone), len(gone)))
		row = self.one_with_a_living_order()
		self.assertEqual(self.order_of(row.name), self.named_in_remark(row))
		for row in gone:
			self.assertFalse(self.order_of(row.name))

	def test_running_it_twice_fills_nothing_more(self):
		_filled, skipped = backfill()
		self.assertEqual(backfill(), (0, skipped))

	def test_a_journal_already_linked_is_left_alone(self):
		row = self.one_with_a_living_order()
		other = frappe.db.get_value("Sales Order", {"name": ["!=", self.named_in_remark(row)]}, "name")
		frappe.db.set_value("Journal Entry", row.name, "lms_sales_order", other, update_modified=False)
		backfill()
		self.assertEqual(self.order_of(row.name), other)

	def test_the_orders_own_pointer_wins_over_the_remark(self):
		pointed = frappe.db.get_value(
			"Sales Order", {"forfeiture_entry": ["is", "set"]}, ["name", "forfeiture_entry"], as_dict=True
		)
		if not pointed:
			self.skipTest("no order names a forfeiture journal on this site")
		other = frappe.db.get_value("Sales Order", {"name": ["!=", pointed.name]}, "name")
		frappe.db.set_value(
			"Journal Entry",
			pointed.forfeiture_entry,
			"user_remark",
			f"Sales Order {other} cancelled before Plot Contract was submitted — changed by the test",
			update_modified=False,
		)
		backfill()
		self.assertEqual(self.order_of(pointed.forfeiture_entry), pointed.name)

	def test_nothing_else_on_a_journal_is_touched(self):
		row = self.one_with_a_living_order()
		watched = ["modified", "modified_by", "docstatus", "user_remark", "total_debit", "posting_date"]
		before = frappe.db.get_value("Journal Entry", row.name, watched)
		backfill()
		self.assertEqual(frappe.db.get_value("Journal Entry", row.name, watched), before)
