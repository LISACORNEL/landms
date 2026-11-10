"""Plot type rates on an approved project (LA-8), editable on screen instead of by script.

Only a System Manager adds, changes or deletes them (operator, 7 Nov 2026); a changed rate is kept
in the project's history and approved plots keep their price; a rate that plots use cannot be
deleted; one row per plot type. Any other save of an approved project is not affected.

Safety (operator, 5 Nov 2026: "we don't want to break the production data"): every write is
rolled back, and a database commit or an outside call (bank, SMS, email gateway) fails the test.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

PROJECT = "Land Acquisition"


def _no_commit():
	raise AssertionError("a database commit happened inside the LA-8 test; nothing may be saved for real")


def _no_outside_call(*args, **kwargs):
	raise AssertionError("an outside call was attempted inside the LA-8 test")


class TestPlotTypeRatesAfterApproval(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=_no_outside_call)
		blocker.start()
		self.addCleanup(blocker.stop)
		plot = frappe.db.get_value(
			"Plot Master",
			{"docstatus": 1, "land_acquisition": ["is", "set"], "plot_type": ["is", "set"]},
			["name", "land_acquisition", "plot_type"],
			as_dict=True,
		)
		if not plot or frappe.db.get_value(PROJECT, plot.land_acquisition, "docstatus") != 1:
			self.skipTest("this site has no approved project with an approved plot")
		self.plot, self.project = plot, plot.land_acquisition

	def _project(self):
		return frappe.get_doc(PROJECT, self.project)

	def _unused_plot_type(self):
		used = {row.plot_type for row in self._project().plot_type_rates}
		name = next((p for p in frappe.get_all("Plot Type", pluck="name") if p not in used), None)
		if name:
			return name
		item = frappe.db.get_value("Plot Type", {}, "item_code")
		return (
			frappe.get_doc(
				{
					"doctype": "Plot Type",
					"plot_type_name": "LA-8 test " + frappe.generate_hash(length=6),
					"item_code": item,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _user(self, roles):
		return (
			frappe.get_doc(
				{
					"doctype": "User",
					"email": f"la8-{frappe.generate_hash(length=8)}@test.invalid",
					"first_name": "LA-8 test",
					"send_welcome_email": 0,
					"roles": [{"role": role} for role in roles],
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def test_the_table_is_editable_after_approval(self):
		self.assertTrue(frappe.get_meta(PROJECT).get_field("plot_type_rates").allow_on_submit)
		row = frappe.get_meta("Land Acquisition Plot Type Rate")
		for field in ("plot_type", "selling_price_per_sqm", "land_rent_rate"):
			self.assertTrue(row.get_field(field).allow_on_submit, field)

	def test_a_system_manager_adds_a_rate(self):
		plot_type = self._unused_plot_type()
		project = self._project()
		project.append(
			"plot_type_rates", {"plot_type": plot_type, "selling_price_per_sqm": 12345, "land_rent_rate": 7}
		)
		project.save()

		stored = frappe.get_all(
			"Land Acquisition Plot Type Rate",
			{"parent": self.project, "plot_type": plot_type},
			pluck="selling_price_per_sqm",
		)
		self.assertEqual(stored, [12345])

	def test_nobody_else_may_change_rates(self):
		plot_type = self._unused_plot_type()
		for roles in (["Land Acquisition Approver"], ["Finance"]):
			frappe.set_user(self._user(roles))
			project = self._project()
			project.append("plot_type_rates", {"plot_type": plot_type, "selling_price_per_sqm": 1})
			with self.assertRaises(frappe.PermissionError, msg=str(roles)):
				project.save()
			frappe.set_user("Administrator")

	def test_a_save_that_does_not_touch_rates_is_never_blocked(self):
		"""Other saves of an approved project (by anyone allowed to make them) pass the rate rule,
		even when the form sends a rate back with rounding noise."""
		project = self._project()
		project._doc_before_save = frappe.get_doc(PROJECT, self.project)
		project.plot_type_rates[0].selling_price_per_sqm = (
			flt(project.plot_type_rates[0].selling_price_per_sqm) + 1e-9
		)
		frappe.set_user(self._user(["Land Acquisition Approver"]))

		project._guard_rate_changes()

	def test_a_changed_rate_is_in_the_history_and_approved_plots_keep_their_price(self):
		before = frappe.db.get_value(
			"Plot Master", self.plot.name, ["selling_price", "selling_price_per_sqm"], as_dict=True
		)
		project = self._project()
		row = next(r for r in project.plot_type_rates if r.plot_type == self.plot.plot_type)
		row.selling_price_per_sqm = flt(row.selling_price_per_sqm) + 1000
		project.save(ignore_version=False)  # tests switch history off by default; a real save keeps it

		after = frappe.db.get_value(
			"Plot Master", self.plot.name, ["selling_price", "selling_price_per_sqm"], as_dict=True
		)
		self.assertEqual(after, before, "an approved plot keeps the price it was made with")
		latest = frappe.get_all(
			"Version",
			{"ref_doctype": PROJECT, "docname": self.project},
			pluck="data",
			order_by="creation desc",
			limit=1,
		)
		self.assertIn('"selling_price_per_sqm"', latest[0], "the change is in the project's history")

	def test_a_rate_that_plots_use_cannot_be_deleted(self):
		project = self._project()
		project.plot_type_rates = [r for r in project.plot_type_rates if r.plot_type != self.plot.plot_type]

		with self.assertRaisesRegex(frappe.ValidationError, "is used by .* plots"):
			project.save()

	def test_a_used_rate_cannot_be_swapped_for_another_plot_type(self):
		project = self._project()
		row = next(r for r in project.plot_type_rates if r.plot_type == self.plot.plot_type)
		row.plot_type = self._unused_plot_type()

		with self.assertRaisesRegex(frappe.ValidationError, "is used by .* plots"):
			project.save()

	def test_an_unused_rate_can_be_deleted(self):
		plot_type = self._unused_plot_type()
		project = self._project()
		project.append("plot_type_rates", {"plot_type": plot_type, "selling_price_per_sqm": 500})
		project.save()

		project = self._project()
		project.plot_type_rates = [r for r in project.plot_type_rates if r.plot_type != plot_type]
		project.save()

		self.assertFalse(
			frappe.db.exists(
				"Land Acquisition Plot Type Rate", {"parent": self.project, "plot_type": plot_type}
			)
		)

	def test_no_plot_type_twice(self):
		project = self._project()
		project.append("plot_type_rates", {"plot_type": self.plot.plot_type, "selling_price_per_sqm": 1})

		with self.assertRaisesRegex(frappe.ValidationError, "appears more than once"):
			project.save()
