"""A plot's delivery note points back at its handover. Real records, every write rolled back."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms.landms.doctype.plot_handover.plot_handover_dashboard import get_data
from landms.patches.backfill_delivery_note_handover import backfill


def _no_commit():
	raise AssertionError("a database commit happened inside the test")


class HandoverCase(FrappeTestCase):
	def setUp(self):
		if not frappe.get_meta("Delivery Note").has_field("plot_handover"):
			self.skipTest("migrate this site first: Delivery Note has no handover link yet")
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=AssertionError("outside call"))
		blocker.start()
		self.addCleanup(blocker.stop)

	def handover_of(self, note):
		return frappe.db.get_value("Delivery Note", note, "plot_handover")


class TestTheLink(HandoverCase):
	def test_the_field_is_a_read_only_link_to_the_handover(self):
		field = frappe.get_meta("Delivery Note").get_field("plot_handover")
		self.assertEqual(
			(field.fieldtype, field.options, field.read_only, field.no_copy), ("Link", "Plot Handover", 1, 1)
		)

	def test_the_handover_lists_its_delivery_notes_under_connections(self):
		self.assertEqual(get_data()["fieldname"], "plot_handover")
		self.assertIn("Delivery Note", get_data()["transactions"][0]["items"])

	def test_submitting_a_handover_makes_a_delivery_note_that_points_back(self):
		draft = self.a_draft_handover_ready_to_submit()
		handover = frappe.get_doc("Plot Handover", draft)
		# the handover's own mid-way commit is held, so the test can roll everything back
		with (
			patch.object(frappe.db, "commit"),
			patch("csf_tz.custom_api.create_trade_in_stock_entry", new=lambda *a, **k: None),
		):
			note = handover._ensure_delivery_note()
		self.assertEqual(frappe.db.get_value("Delivery Note", note, "docstatus"), 1)
		self.assertEqual(self.handover_of(note), handover.name)

	def a_draft_handover_ready_to_submit(self):
		for name, plot in frappe.get_all(
			"Plot Handover", filters={"docstatus": 0}, fields=["name", "plot"], as_list=True
		):
			if frappe.db.get_value("Plot Master", plot, "status") == "Ready for Handover":
				return name
		self.skipTest("this site has no draft handover for a plot ready to hand over")


class TestTheBackfill(HandoverCase):
	def setUp(self):
		super().setUp()
		self.pointers = frappe.get_all(
			"Plot Handover",
			filters={"delivery_note": ["is", "set"], "docstatus": ["!=", 0]},
			fields=["name", "delivery_note"],
		)
		if not self.pointers:
			self.skipTest("this site has no handover with a delivery note")
		note = frappe.qb.DocType("Delivery Note")
		# the state before the backfill: no delivery note knows its handover
		frappe.qb.update(note).set(note.plot_handover, None).run()

	def test_every_note_a_handover_names_is_linked_to_that_handover(self):
		filled, skipped = backfill()
		self.assertEqual(skipped, 0)
		self.assertGreaterEqual(filled, len(self.pointers))
		for row in self.pointers:
			self.assertEqual(self.handover_of(row.delivery_note), row.name)

	def test_running_it_twice_fills_nothing_more(self):
		backfill()
		self.assertEqual(backfill(), (0, 0))

	def test_a_note_already_linked_is_left_alone(self):
		row, other = (
			self.pointers[0],
			frappe.db.get_value("Plot Handover", {"name": ["!=", self.pointers[0].name]}, "name"),
		)
		frappe.db.set_value("Delivery Note", row.delivery_note, "plot_handover", other, update_modified=False)
		backfill()
		self.assertEqual(self.handover_of(row.delivery_note), other)

	def test_nothing_else_on_a_note_is_touched(self):
		watched = ["modified", "modified_by", "docstatus", "customer", "grand_total", "posting_date"]
		before = frappe.db.get_value("Delivery Note", self.pointers[0].delivery_note, watched)
		backfill()
		self.assertEqual(
			frappe.db.get_value("Delivery Note", self.pointers[0].delivery_note, watched), before
		)

	def test_a_draft_that_still_names_an_old_note_does_not_claim_it(self):
		draft = frappe.db.get_value("Plot Handover", {"docstatus": 0}, "name")
		if not draft:
			self.skipTest("this site has no draft handover")
		# an amended draft carries its cancelled original's note
		note = self.pointers[0].delivery_note
		frappe.db.set_value("Plot Handover", draft, "delivery_note", note, update_modified=False)
		self.assertEqual(backfill()[1], 0)
		self.assertEqual(self.handover_of(note), self.pointers[0].name)

	def test_a_note_two_handovers_claim_is_left_for_a_person(self):
		if len(self.pointers) < 2:
			self.skipTest("this site has fewer than two handovers with a delivery note")
		first, second = self.pointers[0], self.pointers[1]
		frappe.db.set_value(
			"Plot Handover", second.name, "delivery_note", first.delivery_note, update_modified=False
		)
		filled, skipped = backfill()
		self.assertEqual(skipped, 1)
		self.assertFalse(self.handover_of(first.delivery_note))

	def forgotten(self):
		"""A submitted handover that never stored its note, with the one note for its plot and order."""
		for candidate in frappe.get_all(
			"Plot Handover",
			filters={"docstatus": 1, "delivery_note": ["is", "not set"]},
			fields=["name", "plot", "contract"],
		):
			order = frappe.db.get_value("Plot Contract", candidate.contract, "sales_order")
			notes = frappe.get_all(
				"Delivery Note Item",
				filters={"serial_no": candidate.plot, "against_sales_order": order, "docstatus": 1},
				pluck="parent",
			)
			if len(set(notes)) == 1:
				return candidate, notes[0]
		self.skipTest("no submitted handover on this site lost its delivery note")

	def test_a_submitted_handover_that_forgot_its_note_is_found_by_plot_and_order(self):
		row, note = self.forgotten()
		backfill()
		self.assertEqual(self.handover_of(note), row.name)
		# the handover itself is not changed: that repair is for a person to decide
		self.assertFalse(frappe.db.get_value("Plot Handover", row.name, "delivery_note"))

	def test_a_note_for_the_same_plot_on_another_order_is_not_taken(self):
		row, note = self.forgotten()
		order = frappe.db.get_value("Plot Contract", row.contract, "sales_order")
		other = frappe.db.get_value("Sales Order", {"name": ["!=", order], "docstatus": 1}, "name")
		frappe.db.set_value(
			"Delivery Note Item", {"parent": note}, "against_sales_order", other, update_modified=False
		)
		backfill()
		self.assertFalse(self.handover_of(note))
