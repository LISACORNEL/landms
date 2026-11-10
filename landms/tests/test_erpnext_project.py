"""Each project's ERPNext Project (LA-6). Every write is rolled back; a commit or an outside call fails the test."""

from unittest.mock import patch

import frappe
from frappe.model.workflow import apply_workflow, get_transitions, get_workflow_name
from frappe.tests.utils import FrappeTestCase
from frappe.utils import today

from landms.landms.doctype.landms_project_setting.landms_project_setting import copy_of_landms_settings
from landms.overrides.project_tag import stamp_project
from landms.patches import create_erpnext_projects

SETTING = "LandMS Project Setting"
_FORWARD = ("Submit for Approval", "Submit", "Approve")
_TAGGED = (
	"Sales Order",
	"Sales Invoice",
	"Payment Entry",
	"Journal Entry",
	"Stock Entry",
	"Delivery Note",
	"Purchase Order",
	"Purchase Invoice",
)


def _no_commit():
	raise AssertionError("a database commit happened inside the LA-6 test; nothing may be saved for real")


def _no_outside_call(*args, **kwargs):
	raise AssertionError("an outside call was attempted inside the LA-6 test")


class TestErpnextProject(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=_no_outside_call)
		blocker.start()
		self.addCleanup(blocker.stop)
		self.company = frappe.db.get_single_value("LandMS Settings", "company") or frappe.db.get_value(
			"Land Acquisition", {"docstatus": 1}, "company"
		)
		if not self.company:
			self.skipTest("this site has no LandMS company")

	def _project(self, name=None):
		return frappe.get_doc(
			{
				"doctype": "Land Acquisition",
				"acquisition_name": name or "LA-6 test " + frappe.generate_hash(length=6),
				"acquisition_date": today(),
				"company": self.company,
				"region": "Dodoma",
				"district": "Test",
				"total_area_sqm": 1000,
				"land_use_type": "Residential",
				"booking_fee_percent": 60,
				"government_share_percent": 28.5,
				"payment_completion_days": 90,
				"collect_via_tcb": 1,
				"payment_channel": "TCB",
				"partner_code": "PART-T" + frappe.generate_hash(length=3).upper(),
			}
		).insert()

	def _fill_settings(self, project, **changes):
		"""A project's setting record is filled before approval (LA-7), the way Finance does it."""
		settings = frappe.get_doc(SETTING, project.name)
		settings.update({**copy_of_landms_settings(self.company), **changes})
		settings.save()
		return settings

	def _submit(self, doc):
		"""Approve the way users do: through the project's workflow when it has one."""
		self._fill_settings(doc)
		if not get_workflow_name(doc.doctype):
			doc.submit()
			return doc
		for _ in range(5):
			doc.reload()
			if doc.docstatus == 1:
				return doc
			actions = [t.action for t in get_transitions(doc)]
			action = next((a for a in _FORWARD if a in actions), None)
			if not action:
				self.fail(f"{doc.name}: no way forward through the workflow (offered {actions})")
			apply_workflow(doc, action)
		self.fail(f"{doc.name} was not approved through its workflow")

	def _erpnext_project(self, project):
		name = frappe.db.get_value(SETTING, project.name, "erpnext_project")
		self.assertTrue(name, f"{project.name} has no ERPNext Project")
		return frappe.get_doc("Project", name)

	def test_saving_a_project_creates_its_erpnext_project(self):
		project = self._project()

		erpnext_project = self._erpnext_project(project)
		self.assertEqual(erpnext_project.project_name, project.acquisition_name)
		self.assertEqual(erpnext_project.company, self.company)
		self.assertEqual(str(erpnext_project.expected_start_date), str(project.acquisition_date))
		self.assertFalse(erpnext_project.expected_end_date, "a land project has no end date")
		self.assertEqual(erpnext_project.status, "Open")

	def test_a_renamed_draft_renames_its_project(self):
		project = self._project()
		project.acquisition_name = "LA-6 renamed " + frappe.generate_hash(length=6)
		project.save()

		self.assertEqual(self._erpnext_project(project).project_name, project.acquisition_name)

	def test_a_name_already_taken_stops_the_save(self):
		taken = "LA-6 taken " + frappe.generate_hash(length=6)
		frappe.get_doc({"doctype": "Project", "project_name": taken, "company": self.company}).insert(
			ignore_mandatory=True
		)

		with self.assertRaisesRegex(frappe.ValidationError, "already exists. Rename this project"):
			self._project(taken)

	def test_the_cost_centre_comes_from_the_projects_own_settings(self):
		project = self._project()
		cost_center = frappe.db.get_value(
			"Cost Center", {"company": self.company, "is_group": 0, "disabled": 0}, "name"
		)
		self._fill_settings(project, cost_center=cost_center)

		self.assertEqual(self._erpnext_project(project).cost_center, cost_center)

	def test_cancel_then_amend_keeps_one_project(self):
		project = self._submit(self._project())
		erpnext_project = self._erpnext_project(project).name

		frappe.get_doc("Land Acquisition", project.name).cancel()
		self.assertEqual(frappe.db.get_value("Project", erpnext_project, "status"), "Cancelled")

		amended = frappe.copy_doc(frappe.get_doc("Land Acquisition", project.name))
		amended.amended_from = project.name
		amended.docstatus = 0
		amended.status = "Draft"  # the copied "Cancelled" is not a project status (separate, older issue)
		amended.insert()

		self.assertEqual(self._erpnext_project(amended).name, erpnext_project)
		self.assertEqual(frappe.db.get_value("Project", erpnext_project, "status"), "Open")
		self.assertEqual(frappe.db.count("Project", {"project_name": project.acquisition_name}), 1)

	def test_deleting_a_draft_deletes_its_project(self):
		project = self._project()
		erpnext_project = self._erpnext_project(project).name

		frappe.delete_doc("Land Acquisition", project.name)

		self.assertFalse(frappe.db.exists("Project", erpnext_project))

	def test_documents_get_the_project_beside_the_tag(self):
		project = self._project()
		erpnext_project = self._erpnext_project(project).name
		other = frappe.get_doc(
			{
				"doctype": "Project",
				"project_name": "LA-6 other " + frappe.generate_hash(length=6),
				"company": self.company,
			}
		).insert(ignore_mandatory=True)

		payment = frappe.new_doc("Payment Entry")
		payment.land_acquisition = project.name
		payment.project = other.name
		frappe.clear_messages()
		stamp_project(payment)
		self.assertEqual(payment.project, erpnext_project, "a different Project is replaced")
		self.assertIn("Project set to", str(frappe.message_log))

		journal = frappe.new_doc("Journal Entry")
		journal.append("accounts", {"land_acquisition": project.name})
		journal.append("accounts", {"project": other.name})
		stamp_project(journal)
		self.assertEqual(journal.accounts[0].project, erpnext_project, "a tagged line gets the Project")
		self.assertEqual(journal.accounts[1].project, other.name, "an untagged line is left alone")

		untagged = frappe.new_doc("Payment Entry")
		untagged.project = other.name
		stamp_project(untagged)
		self.assertEqual(untagged.project, other.name, "a document with no tag is left alone")

	def test_the_rule_never_stops_a_save(self):
		payment = frappe.new_doc("Payment Entry")
		payment.land_acquisition = "LACQ-NO-SUCH"
		with patch("landms.overrides.project_tag._stamp", side_effect=RuntimeError("database gone")):
			stamp_project(payment)
		self.assertFalse(payment.project)

	def test_wired_only_on_the_document_types_landms_tags(self):
		events = frappe.get_module("landms.hooks").doc_events
		self.assertNotIn("*", events, "LandMS never hooks every document of every app")
		wired = {
			doctype
			for doctype, hooks in events.items()
			if "landms.overrides.project_tag.stamp_project" in frappe.as_json(hooks)
		}
		self.assertEqual(wired, set(_TAGGED))

	def test_the_patch_gives_every_live_project_its_erpnext_project(self):
		without = self._project()
		frappe.delete_doc("Project", self._erpnext_project(without).name, ignore_permissions=True, force=True)
		frappe.db.set_value(SETTING, without.name, "erpnext_project", None)
		cancelled = self._project()
		frappe.delete_doc(
			"Project", self._erpnext_project(cancelled).name, ignore_permissions=True, force=True
		)
		frappe.db.set_value(SETTING, cancelled.name, "erpnext_project", None)
		frappe.db.set_value("Land Acquisition", cancelled.name, "docstatus", 2)
		before = frappe.db.count("Project")

		create_erpnext_projects.execute()
		create_erpnext_projects.execute()

		self.assertTrue(frappe.db.get_value(SETTING, without.name, "erpnext_project"))
		self.assertEqual(frappe.db.count("Project"), before + 1, "running twice made extra Projects")
		self.assertFalse(
			frappe.db.get_value(SETTING, cancelled.name, "erpnext_project"),
			"a cancelled project gets no Project",
		)

	def test_the_link_lives_on_the_setting_record_not_on_the_project(self):
		self.assertTrue(frappe.get_meta(SETTING).has_field("erpnext_project"))
		self.assertFalse(frappe.get_meta("Land Acquisition").has_field("erpnext_project"))

	def test_hooks_py_holds_no_events_of_landms_own_doctypes(self):
		"""hooks.py only reaches into other apps."""
		events = frappe.get_module("landms.hooks").doc_events
		own = set(
			frappe.get_all(
				"DocType", filters={"module": ["in", frappe.get_module_list("landms")]}, pluck="name"
			)
		)
		self.assertFalse(own & set(events), f"move these into their controllers: {sorted(own & set(events))}")
