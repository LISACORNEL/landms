"""A plot's cost, price and fees are kept at the precision their fields store. Every write is rolled back."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from landms.landms.doctype.plot_master.plot_master import get_plot_type_selling_rate

WORKED_OUT = (
	"allocated_cost",
	"selling_price",
	"land_rent",
	"premium",
	"registration_prep_fee",
	"stamp_duty",
	"total_govt_fees",
	"total_plot_amount",
)
FEES = (
	"govt_application_fee",
	"premium",
	"land_rent",
	"registration_prep_fee",
	"govt_registration_fee",
	"stamp_duty",
)
MODULE = "landms.landms.doctype.plot_master.plot_master"
# a size and a rate whose cost and price do not land on whole cents
ODD_SIZE = 613.537
ODD_RATE = 6507.31


def _no_commit():
	raise AssertionError("a database commit happened inside the test")


class TestPlotFigures(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=AssertionError("outside call"))
		blocker.start()
		self.addCleanup(blocker.stop)
		self.plot = self.a_new_plot_like_a_real_one()

	def a_new_plot_like_a_real_one(self):
		"""An unsaved plot on a real project that sells, with an awkward size."""
		for like in frappe.get_all(
			"Plot Master",
			filters={"docstatus": 1},
			fields=["land_acquisition", "plot_type"],
			group_by="land_acquisition, plot_type",
		):
			status = frappe.db.get_value("Land Acquisition", like.land_acquisition, "status")
			sells = get_plot_type_selling_rate(like.land_acquisition, like.plot_type) > 0
			if status in ("Approved", "Subdivided") and sells:
				return frappe.get_doc(
					{
						"doctype": "Plot Master",
						"land_acquisition": like.land_acquisition,
						"plot_type": like.plot_type,
						"plot_number": "TEST PLOT FIGURES 613537",
						"plot_size_sqm": ODD_SIZE,
					}
				)
		self.skipTest("this site has no open project with a selling rate")

	def as_stored(self, fieldname, value):
		return flt(value, self.plot.precision(fieldname))

	def test_cost_and_price_keep_their_cents(self):
		with patch(f"{MODULE}.get_plot_type_selling_rate", return_value=ODD_RATE):
			self.plot.validate()
		cost = self.plot.cost_per_sqm * ODD_SIZE
		price = ODD_RATE * ODD_SIZE
		self.assertGreater(self.plot.precision("allocated_cost"), 0)
		for raw in (cost, price):
			self.assertNotEqual(raw, flt(raw, 0))
		self.assertEqual(self.plot.allocated_cost, self.as_stored("allocated_cost", cost))
		self.assertEqual(self.plot.selling_price, self.as_stored("selling_price", price))

	def test_every_worked_out_figure_is_at_its_field_s_precision(self):
		with patch(f"{MODULE}.get_plot_type_selling_rate", return_value=ODD_RATE):
			self.plot.validate()
		for figure in WORKED_OUT:
			value = self.plot.get(figure)
			self.assertGreater(value, 0, figure)
			self.assertEqual(value, self.as_stored(figure, value), figure)

	def test_the_totals_are_the_sum_of_their_parts(self):
		self.plot.validate()
		fees = sum(self.plot.get(fee) for fee in FEES)
		self.assertEqual(self.plot.total_govt_fees, self.as_stored("total_govt_fees", fees))
		self.assertEqual(
			self.plot.total_plot_amount,
			self.as_stored("total_plot_amount", self.plot.selling_price + self.plot.total_govt_fees),
		)

	def test_what_the_form_holds_is_what_the_database_keeps(self):
		self.plot.insert()
		kept = frappe.db.get_value("Plot Master", self.plot.name, list(WORKED_OUT), as_dict=True)
		for figure in WORKED_OUT:
			self.assertEqual(self.plot.get(figure), kept[figure], figure)

	def test_the_tick_saves_straight_after_submit(self):
		settings = frappe.get_single("LandMS Settings")
		if not (settings.plot_inventory_warehouse and settings.land_under_development_account):
			self.skipTest("this site cannot receive a plot into stock")
		self.plot.insert()
		self.plot.submit()
		# the same document the form still holds after submitting, with no reload
		self.plot.no_advance_deadline = 1
		self.plot.save()
		self.assertEqual(frappe.db.get_value("Plot Master", self.plot.name, "no_advance_deadline"), 1)
