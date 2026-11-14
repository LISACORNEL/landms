"""The 90-day tick on a submitted plot: it saves, an unsafe untick is refused, applications follow it."""

import math
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, today

from landms.landms.doctype.plot_master import plot_master

MODULE = "landms.landms.doctype.plot_master.plot_master"
FIGURES = ("cost_per_sqm", "allocated_cost", "selling_price", "total_govt_fees", "total_plot_amount")


def _ensure_tick_field():
	# Production defines the tick as allow-on-submit. A site still on older code
	# may have the column without the field; give this test process the
	# production definition, in memory only.
	meta = frappe.get_meta("Plot Master")
	if not meta.get_field("no_advance_deadline"):
		meta.append(
			"fields",
			{
				"fieldname": "no_advance_deadline",
				"fieldtype": "Check",
				"label": "No Advance Deadline",
				"allow_on_submit": 1,
			},
		)
		meta.init_field_caches()
		if hasattr(meta, "_valid_columns"):
			del meta._valid_columns


class TestPlotTickAfterSubmit(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		if not frappe.db.has_column("Plot Master", "no_advance_deadline"):
			self.skipTest("this site has no 90-day tick column")
		_ensure_tick_field()
		name = frappe.db.get_value("Plot Master", {"docstatus": 1}, "name")
		if not name:
			self.skipTest("this site has no submitted plot")
		self.plot = frappe.get_doc("Plot Master", name)

	# ------------------------------------------------------------------ the save goes through

	def test_the_tick_saves_and_the_figures_stay_as_stored(self):
		before = {figure: self.plot.get(figure) for figure in FIGURES}
		wanted = 0 if self.plot.no_advance_deadline else 1
		self.plot.no_advance_deadline = wanted
		with (
			patch(f"{MODULE}._paid_applications_on", return_value=[]),
			patch(f"{MODULE}._live_applications_on", return_value=[]),
		):
			self.plot.save()
		row = frappe.db.get_value(
			"Plot Master", self.plot.name, ["no_advance_deadline", *FIGURES], as_dict=True
		)
		self.assertEqual(row.no_advance_deadline, wanted)
		self.assertEqual({figure: row[figure] for figure in FIGURES}, before)

	def test_a_change_to_a_locked_field_is_refused(self):
		self.plot.plot_size_sqm = flt(self.plot.plot_size_sqm) + 999
		with self.assertRaises(frappe.UpdateAfterSubmitError):
			self.plot.save()

	def test_a_cost_change_is_refused(self):
		self.plot.allocated_cost = flt(self.plot.allocated_cost) + 1
		with self.assertRaises(frappe.UpdateAfterSubmitError):
			self.plot.save()

	def test_the_noise_helper_other_doctypes_use_respects_the_official_bypass(self):
		from landms.after_submit import drop_rounding_noise_on_locked_fields

		noisy = math.nextafter(flt(self.plot.allocated_cost), math.inf)
		self.plot.allocated_cost = noisy
		self.plot.flags.ignore_validate_update_after_submit = True
		drop_rounding_noise_on_locked_fields(self.plot)
		self.assertEqual(self.plot.allocated_cost, noisy)

	# ------------------------------------------------------------------ the untick guard

	def _untick(self, payment_days_ago, contract_running=False):
		frappe.db.set_value("Plot Master", self.plot.name, "no_advance_deadline", 1, update_modified=False)
		plot = frappe.get_doc("Plot Master", self.plot.name)
		plot.no_advance_deadline = 0
		paid = [
			frappe._dict(
				name="PA-TEST", payment_date=add_days(today(), -payment_days_ago), sales_order="SO-TEST"
			)
		]
		with (
			patch(f"{MODULE}._standard_advance_days", return_value=14),
			patch(f"{MODULE}._paid_applications_on", return_value=paid),
			patch(f"{MODULE}._has_running_contract", return_value=contract_running),
			patch(f"{MODULE}._live_applications_on", return_value=[]),
		):
			plot.save()

	def test_removing_the_tick_is_refused_when_tonight_would_expire_the_order(self):
		with self.assertRaisesRegex(frappe.ValidationError, "tonight's run would expire"):
			self._untick(payment_days_ago=30)

	def test_removing_the_tick_is_refused_on_the_last_day_of_the_window(self):
		# window ends today; the run starts at 01:00 tomorrow and would expire it
		with self.assertRaisesRegex(frappe.ValidationError, "tonight's run would expire"):
			self._untick(payment_days_ago=14)

	def test_removing_the_tick_is_allowed_while_the_standard_window_is_open(self):
		self._untick(payment_days_ago=13)  # window ends tomorrow
		self.assertEqual(frappe.db.get_value("Plot Master", self.plot.name, "no_advance_deadline"), 0)

	def test_removing_the_tick_is_allowed_when_the_contract_is_already_running(self):
		self._untick(payment_days_ago=30, contract_running=True)
		self.assertEqual(frappe.db.get_value("Plot Master", self.plot.name, "no_advance_deadline"), 0)

	# ------------------------------------------------------------------ applications follow the tick

	def test_live_applications_follow_the_tick(self):
		paid_on = add_days(today(), -3)
		live = [
			frappe._dict(name="PA-LIVE", payment_date=paid_on),
			frappe._dict(name="PA-UNPAID", payment_date=None),
		]
		plot = frappe._dict(no_advance_deadline=1, payment_completion_days=90)
		with (
			patch(f"{MODULE}._live_applications_on", return_value=live),
			patch(f"{MODULE}.frappe.db.set_value") as set_value,
		):
			plot_master.PlotMaster._update_live_application_windows(frappe._dict(name="P", **plot))
		calls = {c.args[1]: c.args[2] for c in set_value.call_args_list}
		self.assertEqual(calls["PA-LIVE"], {"validity_days": 90, "expiry_date": add_days(paid_on, 90)})
		self.assertEqual(calls["PA-UNPAID"], {"validity_days": 90})

	def test_without_the_tick_applications_get_the_standard_window(self):
		with patch(f"{MODULE}._standard_advance_days", return_value=14):
			self.assertEqual(
				plot_master._advance_days_for(
					frappe._dict(no_advance_deadline=0, payment_completion_days=90)
				),
				14,
			)
			self.assertEqual(
				plot_master._advance_days_for(frappe._dict(no_advance_deadline=1, payment_completion_days=0)),
				14,
			)
			self.assertEqual(
				plot_master._advance_days_for(
					frappe._dict(no_advance_deadline=1, payment_completion_days=90)
				),
				90,
			)
