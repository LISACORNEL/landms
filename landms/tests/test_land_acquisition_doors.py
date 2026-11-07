"""The project's web doors (LA-1, part 2).

Before: four functions behind the project form were open to anyone with a
login, as plain links: the full cost breakdown, two refreshes that write onto
the project, and Recalculate Plot Costs, which rewrites the stock value of
every unsold plot. Now Recalculate is System Manager only; the form's two
refreshes go through new doors that need read permission on the project; the
breakdown and the internal refresh functions are no web doors at all; and
every door left accepts POST only, so a link in a message cannot fire it.

Safety (operator, 5 Nov 2026): every write is rolled back; a database commit or
an outside call fails the test instead of happening; Recalculate is only ever
reached on a draft project, where it stops before saving or queueing anything.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import today

from landms.landms.doctype.land_acquisition import land_acquisition as project_code

_NOT_A_ROLE_TO_TEST = ("System Manager", "Administrator", "All", "Guest")


def _no_commit():
	raise AssertionError("a database commit happened inside the doors test; nothing may be saved for real")


def _no_outside_call(*args, **kwargs):
	raise AssertionError("an outside call was attempted inside the doors test")


class TestLandAcquisitionDoors(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=_no_outside_call)
		blocker.start()
		self.addCleanup(blocker.stop)
		self.addCleanup(frappe.set_user, frappe.session.user)

		company = frappe.db.get_value("Land Acquisition", {"docstatus": 1}, "company") or frappe.db.get_value(
			"Company", {}, "name"
		)
		if not company:
			self.skipTest("this site has no company")
		# A draft: Recalculate refuses it before saving or queueing anything.
		self.project = frappe.get_doc(
			{
				"doctype": "Land Acquisition",
				"acquisition_name": "LA-1 doors test " + frappe.generate_hash(length=6),
				"acquisition_date": today(),
				"company": company,
				"region": "Dodoma",
				"district": "Test",
				"total_area_sqm": 1000,
				"land_use_type": "Residential",
				"booking_fee_percent": 60,
				"government_share_percent": 28.5,
				"payment_completion_days": 90,
				"collect_via_tcb": 1,
				"payment_channel": "TCB",
				"partner_code": "PART-D" + frappe.generate_hash(length=3).upper(),
			}
		).insert()
		self.outsider = self._user(["Accounts User"])
		self.manager = self._user(["System Manager"])
		self.assertFalse(
			frappe.has_permission("Land Acquisition", "read", self.project.name, user=self.outsider)
		)

	def _user(self, roles):
		"""A fresh throwaway login with exactly these roles, saved once.

		Saved once on purpose: a second save of a user re-saves its Contact without
		skipping mandatory fields, and live makes the Contact phone mandatory.
		"""
		return (
			frappe.get_doc(
				{
					"doctype": "User",
					"email": f"la1-doors-{frappe.generate_hash(length=8)}@test.invalid",
					"first_name": "LA-1 doors test",
					"send_welcome_email": 0,
					"roles": [{"role": role} for role in roles],
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _reader(self):
		"""A user whose only project right is the one a read-only role gives on this site."""
		perms = frappe.get_all(
			"Custom DocPerm", filters={"parent": "Land Acquisition", "read": 1}, fields=["role", "write"]
		) or frappe.get_all(
			"DocPerm", filters={"parent": "Land Acquisition", "read": 1}, fields=["role", "write"]
		)
		roles = sorted((p.write, p.role) for p in perms if p.role not in _NOT_A_ROLE_TO_TEST)
		if not roles:
			self.skipTest("no role other than System Manager may open projects on this site")
		return self._user([roles[0][1]])

	def test_recalculate_refuses_anyone_but_a_system_manager(self):
		frappe.set_user(self.outsider)
		with self.assertRaisesRegex(frappe.PermissionError, "Only a System Manager"):
			project_code.recalculate_plot_costs(self.project.name)

	def test_recalculate_refuses_people_who_may_only_open_the_project(self):
		frappe.set_user(self._reader())
		with self.assertRaisesRegex(frappe.PermissionError, "Only a System Manager"):
			project_code.recalculate_plot_costs(self.project.name)

	def test_recalculate_lets_a_system_manager_through_to_its_own_checks(self):
		frappe.set_user(self.manager)
		with self.assertRaisesRegex(frappe.ValidationError, "must be submitted"):
			project_code.recalculate_plot_costs(self.project.name)
		self.assertFalse(
			frappe.db.get_value("Land Acquisition", self.project.name, "recalculation_in_progress")
		)

	def test_the_form_doors_refuse_people_who_cannot_open_the_project(self):
		frappe.set_user(self.outsider)
		for door in (project_code.refresh_cost_summary, project_code.refresh_plot_summary):
			with self.subTest(door=door.__name__), self.assertRaises(frappe.PermissionError):
				door(self.project.name)

	def test_the_form_doors_answer_people_who_may_only_open_the_project(self):
		frappe.set_user(self._reader())
		self.assertIn("totals", project_code.refresh_cost_summary(self.project.name))
		self.assertIn("total_plots", project_code.refresh_plot_summary(self.project.name))

	def test_only_the_form_doors_and_recalculate_are_web_doors_and_post_only(self):
		for internal in (
			project_code.sync_land_acquisition_cost_summary,
			project_code.get_land_acquisition_cost_summary,
			project_code.sync_land_acquisition_plot_summary,
		):
			self.assertNotIn(internal, frappe.whitelisted, internal.__name__)
		for door in (
			project_code.refresh_cost_summary,
			project_code.refresh_plot_summary,
			project_code.recalculate_plot_costs,
		):
			self.assertIn(door, frappe.whitelisted, door.__name__)
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[door], ["POST"], door.__name__)
