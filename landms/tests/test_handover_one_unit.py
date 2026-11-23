"""A handover's submit saves everything together or nothing at all. Real records, every write rolled back."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

MODULE = "landms.landms.doctype.plot_handover.plot_handover"


def _no_commit():
	raise AssertionError("a database commit happened inside the handover's submit")


class TestHandoverOneUnit(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		for blocked in (
			patch("requests.sessions.Session.request", side_effect=AssertionError("outside call")),
			# the laptop's CSF TZ has an invoice check live does not have
			patch("csf_tz.custom_api.create_trade_in_stock_entry", new=lambda *a, **k: None),
		):
			blocked.start()
			self.addCleanup(blocked.stop)
		self.handover = self.a_draft_ready_to_hand_over()

	def a_draft_ready_to_hand_over(self):
		for name, plot in frappe.get_all(
			"Plot Handover", filters={"docstatus": 0}, fields=["name", "plot"], as_list=True
		):
			if frappe.db.get_value("Plot Master", plot, "status") == "Ready for Handover":
				handover = frappe.get_doc("Plot Handover", name)
				handover.received_by = handover.received_by or handover.customer
				return handover
		self.skipTest("this site has no draft handover for a plot ready to hand over")

	def notes_for_the_plot(self):
		return frappe.get_all(
			"Delivery Note Item",
			filters={"serial_no": self.handover.plot, "docstatus": ["<", 2]},
			pluck="parent",
		)

	def test_submitting_saves_nothing_to_the_database_before_the_end(self):
		self.handover.submit()
		self.assertEqual(frappe.db.get_value("Plot Handover", self.handover.name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Plot Master", self.handover.plot, "status"), "Delivered")
		self.assertEqual(frappe.db.get_value("Delivery Note", self.handover.delivery_note, "docstatus"), 1)

	def test_a_failure_after_the_delivery_note_leaves_nothing_behind(self):
		before = self.notes_for_the_plot()
		frappe.db.savepoint("before_submit")
		with (
			patch(f"{MODULE}.set_plot_status", side_effect=frappe.ValidationError("a later step failed")),
			self.assertRaises(frappe.ValidationError),
		):
			self.handover.submit()
		frappe.db.rollback(save_point="before_submit")
		self.assertEqual(frappe.db.get_value("Plot Handover", self.handover.name, "docstatus"), 0)
		self.assertEqual(
			frappe.db.get_value("Plot Master", self.handover.plot, "status"), "Ready for Handover"
		)
		self.assertEqual(self.notes_for_the_plot(), before)
