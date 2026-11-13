"""A plot's stock receipt points back at its plot. Real records, every write rolled back."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms.landms.doctype.plot_master.plot_master_dashboard import get_data
from landms.patches.backfill_stock_entry_plot import backfill


def _no_commit():
	raise AssertionError("a database commit happened inside the test")


class TestStockEntryPlotLink(FrappeTestCase):
	def setUp(self):
		if not frappe.get_meta("Stock Entry").has_field("plot"):
			self.skipTest("migrate this site first: Stock Entry has no Plot field yet")
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		p = patch("requests.sessions.Session.request", side_effect=AssertionError("outside call"))
		p.start()
		self.addCleanup(p.stop)
		plots = frappe.get_all(
			"Plot Master",
			filters={"docstatus": 1, "status": "Available", "stock_entry": ["is", "set"]},
			fields=["name", "stock_entry", "land_acquisition", "plot_type", "plot_size_sqm"],
			order_by="name",
			limit=1,
		)
		if not plots:
			self.skipTest("this site has no available plot in stock")
		self.plot = plots[0]

	def unlink_every_receipt(self):
		"""The state before the backfill: no receipt knows its plot."""
		entry = frappe.qb.DocType("Stock Entry")
		frappe.qb.update(entry).set(entry.plot, None).run()

	def test_the_field_is_a_read_only_link_to_the_plot(self):
		field = frappe.get_meta("Stock Entry").get_field("plot")
		self.assertEqual((field.fieldtype, field.options, field.read_only), ("Link", "Plot Master", 1))

	def test_the_backfill_links_each_receipt_to_its_own_plot(self):
		self.unlink_every_receipt()
		filled, skipped = backfill()
		self.assertGreater(filled, 0)
		self.assertEqual(skipped, 0)
		self.assertEqual(frappe.db.get_value("Stock Entry", self.plot.stock_entry, "plot"), self.plot.name)
		# every plot in stock is found again from its receipt, and by no other receipt
		in_stock = frappe.get_all(
			"Plot Master", filters={"stock_entry": ["is", "set"]}, fields=["name", "stock_entry"]
		)
		linked = dict(
			frappe.get_all(
				"Stock Entry",
				filters={"name": ["in", [p.stock_entry for p in in_stock]]},
				fields=["name", "plot"],
				as_list=True,
			)
		)
		self.assertEqual({p.stock_entry: p.name for p in in_stock}, linked)

	def test_the_backfill_is_safe_to_run_twice(self):
		self.unlink_every_receipt()
		backfill()
		self.assertEqual(backfill(), (0, 0))

	def test_the_backfill_changes_nothing_else_on_a_receipt(self):
		self.unlink_every_receipt()
		before = frappe.db.get_value(
			"Stock Entry", self.plot.stock_entry, ["modified", "docstatus", "total_amount"]
		)
		backfill()
		self.assertEqual(
			frappe.db.get_value(
				"Stock Entry", self.plot.stock_entry, ["modified", "docstatus", "total_amount"]
			),
			before,
		)

	def test_a_stock_entry_that_is_not_a_plot_receipt_is_left_alone(self):
		self.unlink_every_receipt()
		plots = frappe.get_all("Plot Master", pluck="name")
		entry, line = frappe.qb.DocType("Stock Entry"), frappe.qb.DocType("Stock Entry Detail")
		others = (
			frappe.qb.from_(entry)
			.inner_join(line)
			.on(line.parent == entry.name)
			.select(entry.name)
			.where(line.serial_no.notin(plots) | line.serial_no.isnull())
			.limit(50)
			.run(pluck=True)
		)
		if not others:
			self.skipTest("this site has no other stock entry")
		backfill()
		self.assertEqual(frappe.db.count("Stock Entry", {"name": ["in", others], "plot": ["is", "set"]}), 0)

	def test_a_new_plot_s_receipt_points_at_it_and_both_cancel_together(self):
		number = f"PLOT NO 9{frappe.generate_hash(length=5).upper()} TEST"
		plot = frappe.get_doc(
			{
				"doctype": "Plot Master",
				"land_acquisition": self.plot.land_acquisition,
				"plot_number": number,
				"plot_type": self.plot.plot_type,
				"plot_size_sqm": self.plot.plot_size_sqm,
			}
		).insert(ignore_permissions=True)
		plot.submit()
		plot.reload()
		self.assertEqual(
			frappe.db.get_value("Stock Entry", plot.stock_entry, ["plot", "docstatus"]), (plot.name, 1)
		)
		self.assertEqual(
			frappe.get_all("Stock Entry", filters={"plot": plot.name}, pluck="name"), [plot.stock_entry]
		)

		receipt = plot.stock_entry
		plot.cancel()
		self.assertEqual(frappe.db.get_value("Plot Master", plot.name, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Stock Entry", receipt, ["plot", "docstatus"]), (plot.name, 2))

	def test_a_plot_s_receipt_cannot_be_cancelled_on_its_own(self):
		with self.assertRaises(frappe.LinkExistsError):
			frappe.get_doc("Stock Entry", self.plot.stock_entry).cancel()


class TestPlotConnections(FrappeTestCase):
	def test_a_plot_lists_its_stock_entries_under_connections(self):
		data = get_data()
		self.assertEqual(data["fieldname"], "plot")
		self.assertIn("Stock Entry", [item for group in data["transactions"] for item in group["items"]])
