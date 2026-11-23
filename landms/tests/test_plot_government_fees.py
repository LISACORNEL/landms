"""A plot's government fee figures come from its project's setting. Real records, every write rolled back."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms.landms.doctype.plot_master.plot_master import USUAL_GOVERNMENT_FEES, government_fee_figures

SETTING = "LandMS Project Setting"
FIELD = "govt_appl_fee_amount"


def _no_commit():
	raise AssertionError("a database commit happened inside the test")


class TestPlotGovernmentFees(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=AssertionError("outside call"))
		blocker.start()
		self.addCleanup(blocker.stop)
		self.plot = frappe.db.get_value(
			"Plot Master",
			{"docstatus": 1, "land_acquisition": ["is", "set"]},
			["name", "land_acquisition"],
			as_dict=True,
		)
		if not self.plot or not frappe.db.exists(SETTING, self.plot.land_acquisition):
			self.skipTest("this site has no plot on a project with a setting record")
		self.project = self.plot.land_acquisition

	def set_figure(self, doctype, name, value):
		frappe.db.set_value(doctype, name, FIELD, value)
		frappe.clear_document_cache(doctype, name)

	def worked_out(self):
		plot = frappe.get_doc("Plot Master", self.plot.name)
		plot.fill_sales_defaults()
		plot.fill_financials()
		return plot

	def test_the_plot_follows_its_project(self):
		self.set_figure(SETTING, self.project, 7000)
		self.assertEqual(self.worked_out().govt_application_fee, 7000)

	def test_an_empty_project_figure_falls_back_to_landms_settings(self):
		self.set_figure(SETTING, self.project, 0)
		self.set_figure("LandMS Settings", "LandMS Settings", 6100)
		self.assertEqual(government_fee_figures(self.project)[FIELD], 6100)

	def test_with_neither_the_usual_figure_is_used(self):
		self.set_figure(SETTING, self.project, 0)
		self.set_figure("LandMS Settings", "LandMS Settings", 0)
		self.assertEqual(government_fee_figures(self.project)[FIELD], USUAL_GOVERNMENT_FEES[FIELD])

	def test_a_project_without_a_setting_record_uses_landms_settings(self):
		self.set_figure("LandMS Settings", "LandMS Settings", 6200)
		self.assertEqual(government_fee_figures("NO-SUCH-PROJECT")[FIELD], 6200)
		self.assertEqual(government_fee_figures(None)[FIELD], 6200)

	def test_a_project_figure_reaches_the_plot_totals(self):
		before = self.worked_out()
		self.set_figure(SETTING, self.project, before.govt_application_fee + 1000)
		after = self.worked_out()
		self.assertAlmostEqual(after.total_govt_fees - before.total_govt_fees, 1000, places=2)
		self.assertAlmostEqual(after.total_plot_amount - before.total_plot_amount, 1000, places=2)
