"""The project's cost totals and plot counts, read with the query builder instead of raw SQL (LA-3).

The queries changed form only: on a copy of live, the raw SQL and the query builder versions
returned identical rows for every project, supplier invoice and journal. Live has no purchase
orders tagged to a project yet, so those two queries are proven here on a built example: one
purchase order with lines for two projects and one untagged line, and an advance paid on it.

Safety (operator, 5 Nov 2026: "we don't want to break the production data"): every write is
rolled back, and a database commit or an outside call (bank, SMS, email gateway) fails the test.
"""

import os
import re
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, today

from landms.landms.doctype.land_acquisition import cost_totals
from landms.landms.doctype.land_acquisition import land_acquisition as form

PROJECT_FILES = (
	("landms", "doctype", "land_acquisition", "land_acquisition.py"),
	("landms", "doctype", "land_acquisition", "cost_totals.py"),
	("landms", "doctype", "land_acquisition", "recalculate_plot_costs.py"),
	("overrides", "update_project_costs.py"),
)


def _no_commit():
	raise AssertionError("a database commit happened inside the LA-3 test; nothing may be saved for real")


def _no_outside_call(*args, **kwargs):
	raise AssertionError("an outside call was attempted inside the LA-3 test")


class TestProjectCostsNoRawSql(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=_no_outside_call)
		blocker.start()
		self.addCleanup(blocker.stop)

	def test_no_raw_sql_in_the_project_code(self):
		for parts in PROJECT_FILES:
			source = open(frappe.get_app_path("landms", *parts)).read()
			self.assertFalse(re.search(r"frappe\.db\.sql\b", source), os.path.join(*parts))

	def _two_project_order_with_an_advance(self):
		"""A purchase order: 3,000,000 for project A, 1,000,000 for project B, 500,000 untagged;
		then 900,000 paid on it in advance, which belongs 3/4.5 to A and 1/4.5 to B."""
		projects = frappe.get_all(
			"Land Acquisition", {"docstatus": ["<", 2]}, pluck="name", order_by="name", limit=2
		)
		if len(projects) < 2:
			self.skipTest("this site has fewer than two projects")
		a, b = projects
		company = frappe.db.get_value("Land Acquisition", a, "company")
		supplier = frappe.db.get_value("Supplier", {"disabled": 0, "on_hold": 0}, "name")
		item = frappe.db.get_value(
			"Item", {"is_stock_item": 0, "is_purchase_item": 1, "disabled": 0, "has_variants": 0}, "name"
		)
		cost_center = frappe.get_cached_value("Company", company, "cost_center") or frappe.db.get_value(
			"Cost Center", {"company": company, "is_group": 0, "disabled": 0}, "name"
		)
		bank = frappe.db.get_value(
			"Account", {"company": company, "account_type": "Bank", "is_group": 0, "disabled": 0}, "name"
		)
		payable = frappe.get_cached_value("Company", company, "default_payable_account")
		if not (supplier and item and cost_center and bank and payable):
			self.skipTest("this site lacks a supplier, item, cost centre, bank or payable account")

		def line(rate, project=None):
			return {
				"item_code": item,
				"qty": 1,
				"rate": rate,
				"schedule_date": today(),
				"land_acquisition": project,
				"cost_center": cost_center,
			}

		order = frappe.get_doc(
			{
				"doctype": "Purchase Order",
				"supplier": supplier,
				"company": company,
				"transaction_date": today(),
				"schedule_date": today(),
				"cost_center": cost_center,
				"items": [line(3000000, a), line(1000000, b), line(500000)],
			}
		).insert()
		order.submit()
		payment = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Pay",
				"company": company,
				"posting_date": today(),
				"party_type": "Supplier",
				"party": supplier,
				"paid_from": bank,
				"paid_to": payable,
				"paid_amount": 900000,
				"received_amount": 900000,
				"reference_no": "LA-3 test",
				"reference_date": today(),
				"cost_center": cost_center,
				"references": [
					{
						"reference_doctype": "Purchase Order",
						"reference_name": order.name,
						"allocated_amount": 900000,
					}
				],
			}
		).insert()
		payment.submit()
		return a, b, order.name

	def test_committed_per_project_on_a_shared_purchase_order(self):
		a, b, order = self._two_project_order_with_an_advance()

		for project, expected in ((a, 3000000), (b, 1000000)):
			rows = [r for r in cost_totals._fetch_committed(project) if r["po"] == order]
			self.assertEqual(len(rows), 1, project)
			self.assertEqual(flt(rows[0]["amount"]), expected, project)

	def test_an_advance_is_shared_by_each_projects_part_of_the_order(self):
		a, b, order = self._two_project_order_with_an_advance()

		for project, expected in ((a, 600000), (b, 200000)):
			rows = [r for r in cost_totals._fetch_paid_advance(project) if r["po"] == order]
			self.assertEqual(len(rows), 1, project)
			self.assertAlmostEqual(flt(rows[0]["amount"]), expected, places=2, msg=project)
		self.assertEqual(cost_totals._fetch_paid_advance("LACQ-NO-SUCH"), [])

	def test_the_plot_counts_match_the_plots(self):
		project = frappe.db.get_value("Land Acquisition", {"docstatus": 1}, "name")
		if not project:
			self.skipTest("this site has no approved project")
		submitted = {"land_acquisition": project, "docstatus": 1}

		counts = form.sync_land_acquisition_plot_summary(project)

		self.assertEqual(counts["total_plots"], frappe.db.count("Plot Master", submitted))
		self.assertEqual(
			counts["available_plots"], frappe.db.count("Plot Master", {**submitted, "status": "Available"})
		)
		self.assertEqual(
			counts["delivered_plots"],
			frappe.db.count("Plot Master", {**submitted, "status": ["in", ["Delivered", "Title Closed"]]}),
		)
