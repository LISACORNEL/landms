"""One settings record per project (LA-7).

The record holds what LandMS Settings holds except the receipt verification address and the TCB
log series. A new project gets its record empty; the project cannot be approved while a field
marked * is empty (0 counts as empty for the days and the forfeiture %, not for the fee amount);
the government fees are informational and never block. Projects that existed before get an exact
copy of LandMS Settings from the patch. Deleting a draft project deletes its record. The record is
hidden and only System Manager and Finance may change it. LandMS Settings itself is left as it is.

Safety (operator, 5 Nov 2026: "we don't want to break the production data"): every write is
rolled back, and a database commit or an outside call (bank, SMS, email gateway) fails the test.
"""

from unittest.mock import patch

import frappe
from frappe.model import no_value_fields
from frappe.model.workflow import apply_workflow, get_transitions, get_workflow_name
from frappe.tests.utils import FrappeTestCase
from frappe.utils import today

from landms.landms.doctype.landms_project_setting.landms_project_setting import (
	accounts_for,
	copy_of_landms_settings,
)
from landms.patches import create_project_settings

SETTINGS = "LandMS Project Setting"
_FORWARD = ("Submit for Approval", "Submit", "Approve")
# the asterisks of LandMS Settings, plus "Fee Received In" (Lisa, 5 Nov 2026)
REQUIRED = {
	"land_acquisition",
	"company",
	"default_currency",
	"plot_inventory_warehouse",
	"cost_center",
	"land_under_development_account",
	"plot_inventory_account",
	"tcb_bank_account",
	"revenue_account",
	"cogs_account",
	"customer_advance_account",
	"government_payable_account",
	"seller_payable_account",
	"forfeited_deposits_account",
	"forfeiture_percentage",
	"application_fee_item",
	"application_fee_amount",
	"unpaid_application_expiry_days",
	"application_fee_validity_days",
	"application_fee_income_account",
	"application_fee_receiving_account",
}


def _no_commit():
	raise AssertionError("a database commit happened inside the LA-7 test; nothing may be saved for real")


def _no_outside_call(*args, **kwargs):
	raise AssertionError("an outside call was attempted inside the LA-7 test")


def _value_fields(doctype):
	return [df for df in frappe.get_meta(doctype).fields if df.fieldtype not in no_value_fields]


class TestProjectSettings(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=_no_outside_call)
		blocker.start()
		self.addCleanup(blocker.stop)
		self.company = frappe.db.get_single_value("LandMS Settings", "company")
		if not self.company:
			self.skipTest("this site has no LandMS company")

	def _project(self, **values):
		return frappe.get_doc(
			{
				"doctype": "Land Acquisition",
				"acquisition_name": "LA-7 test " + frappe.generate_hash(length=6),
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
				**values,
			}
		).insert()

	def _fill(self, name, **changes):
		"""Finance fills the record: here with today's LandMS Settings values, then any changes."""
		record = frappe.get_doc(SETTINGS, name)
		record.update({**copy_of_landms_settings(self.company), **changes})
		record.save()
		return record

	def _submit(self, doc):
		"""Submit the way users do: through the project's approval workflow when it has one."""
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
		self.fail(f"{doc.name} was not submitted through its workflow")

	def _user(self, roles):
		return (
			frappe.get_doc(
				{
					"doctype": "User",
					"email": f"la7-{frappe.generate_hash(length=8)}@test.invalid",
					"first_name": "LA-7 test",
					"send_welcome_email": 0,
					"roles": [{"role": role} for role in roles],
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def test_a_new_project_gets_an_empty_record(self):
		project = self._project()

		record = frappe.get_doc(SETTINGS, project.name)
		self.assertEqual(record.land_acquisition, project.name)
		self.assertEqual(record.project_name, project.acquisition_name)
		self.assertEqual(record.get_title(), project.acquisition_name, "the title is the project's name")
		self.assertEqual(record.company, self.company)
		# the system fills the ERPNext Project; Finance fills the rest
		self.assertTrue(record.erpnext_project)
		for df in _value_fields(SETTINGS):
			if df.fieldname not in ("land_acquisition", "project_name", "company", "erpnext_project"):
				self.assertFalse(record.get(df.fieldname), f"{df.fieldname} should start empty")

	def test_one_record_per_project(self):
		project = self._project()

		with self.assertRaises(frappe.DuplicateEntryError):
			frappe.get_doc({"doctype": SETTINGS, "land_acquisition": project.name}).insert(
				ignore_permissions=True, ignore_mandatory=True
			)

	def test_the_form(self):
		today_fields = {
			df.fieldname for df in _value_fields("LandMS Settings") if not df.get("is_custom_field")
		}
		fields = _value_fields(SETTINGS)
		meta = frappe.get_meta(SETTINGS)

		self.assertEqual(
			today_fields - {df.fieldname for df in fields},
			{"receipt_verification_address", "naming_series_tcb_log"},
			"everything of LandMS Settings except the receipt address and the TCB log series",
		)
		self.assertEqual({df.fieldname for df in fields if df.reqd}, REQUIRED)
		self.assertFalse([df.fieldname for df in fields if df.fieldname.startswith("govt") and df.reqd])
		self.assertFalse([df.fieldname for df in fields if df.default], "nothing is pre-filled")
		self.assertEqual(
			[df.label for df in meta.fields if df.fieldtype == "Tab Break"],
			["General", "Accounts", "Application Fee", "Government Fees (Informational)", "Naming Series"],
		)
		self.assertEqual([df.fieldname for df in fields][:2], ["project_name", "land_acquisition"])

	def test_approval_waits_for_the_starred_fields(self):
		project = self._project()

		with self.assertRaises(frappe.ValidationError) as refused:
			self._submit(project)
		message = str(refused.exception)
		for label in ("Revenue", "Cost Centre", "Fee Received In", "Unpaid Expiry (Days)", "Forfeiture %"):
			self.assertIn(label, message)
		for label in ("Registration Fee", "Premium Rate", "Malipopay Bank Account", "Contract Series"):
			self.assertNotIn(label, message, "optional and informational fields never block")
		self.assertEqual(frappe.db.get_value("Land Acquisition", project.name, "docstatus"), 0)

		self._fill(project.name, govt_appl_fee_amount=0, govt_registration_fee_amount=0, govt_premium_rate=0)
		self.assertEqual(self._submit(frappe.get_doc("Land Acquisition", project.name)).docstatus, 1)

	def test_zero_days_count_as_empty_but_a_zero_fee_does_not(self):
		project = self._project()
		self._fill(project.name, unpaid_application_expiry_days=0, application_fee_amount=0)

		with self.assertRaises(frappe.ValidationError) as refused:
			self._submit(project)
		self.assertIn("Unpaid Expiry (Days)", str(refused.exception))
		self.assertNotIn("Fee Amount", str(refused.exception), "live charges 0 today; 0 is allowed")

	def test_a_link_of_another_company_is_refused(self):
		other = frappe.db.get_value(
			"Account", {"company": ["!=", self.company], "is_group": 0, "disabled": 0}, "name"
		)
		if not other:
			self.skipTest("this site has one company only")
		project = self._project()

		with self.assertRaisesRegex(frappe.ValidationError, "belongs to another company"):
			self._fill(project.name, revenue_account=other)

	def test_a_renamed_draft_project_shows_its_new_name(self):
		project = self._project()
		project.acquisition_name = "LA-7 renamed " + frappe.generate_hash(length=6)
		project.save()

		self.assertEqual(
			frappe.db.get_value(SETTINGS, project.name, "project_name"), project.acquisition_name
		)

	def test_deleting_a_draft_project_deletes_its_record(self):
		project = self._project()

		frappe.delete_doc("Land Acquisition", project.name)

		self.assertFalse(frappe.db.exists(SETTINGS, project.name))

	def test_accounts_for(self):
		project = self._project()

		self.assertEqual(accounts_for(project.name).land_acquisition, project.name)
		with self.assertRaisesRegex(frappe.ValidationError, "has no settings yet"):
			accounts_for("LACQ-NO-SUCH-PROJECT")

	def test_hidden_and_finance_owned(self):
		meta = frappe.get_meta(SETTINGS)
		self.assertTrue(meta.read_only, "must not be searchable")
		self.assertTrue(meta.in_create, "must not be created by hand")
		self.assertFalse(frappe.db.exists("Workspace Link", {"link_to": SETTINGS}))
		self.assertFalse(any(p.create or p.delete for p in meta.permissions))
		self.assertEqual({p.role for p in meta.permissions if p.write}, {"System Manager", "Finance"})

		record = self._project().name
		finance = self._user(["Finance"])
		accounts = self._user(["Accounts Manager"])
		sales = self._user(["Sales User"])
		self.assertTrue(frappe.has_permission(SETTINGS, "write", doc=record, user=finance))
		self.assertTrue(frappe.has_permission(SETTINGS, "read", doc=record, user=accounts))
		self.assertFalse(frappe.has_permission(SETTINGS, "write", doc=record, user=accounts))
		self.assertFalse(frappe.has_permission(SETTINGS, "read", doc=record, user=sales))

	def test_changing_an_account_after_postings_is_said_out_loud(self):
		if not frappe.db.has_column("GL Entry", "land_acquisition"):
			self.skipTest("ledger rows carry no project on this site")
		project = frappe.db.get_value(
			"GL Entry", {"is_cancelled": 0, "land_acquisition": ["is", "set"]}, "land_acquisition"
		)
		if not project or not frappe.db.exists(SETTINGS, project):
			self.skipTest("no project with postings and a settings record on this site")
		record = frappe.get_doc(SETTINGS, project)
		other = frappe.db.get_value(
			"Account",
			{"company": record.company, "is_group": 0, "name": ["!=", record.revenue_account or ""]},
			"name",
		)
		frappe.clear_messages()
		record.revenue_account = other
		record.save()

		self.assertIn("already has postings", str(frappe.message_log))

	def test_the_patch_copies_landms_settings_exactly(self):
		without = self._project()
		frappe.delete_doc(SETTINGS, without.name, ignore_permissions=True)
		hand_filled = self._fill(self._project().name, forfeiture_percentage=12.5)
		cancelled = [
			name
			for name in frappe.get_all("Land Acquisition", {"docstatus": 2}, pluck="name")
			if not frappe.db.exists(SETTINGS, name)
		]
		today_settings = frappe.get_single("LandMS Settings").as_dict()
		before = frappe.db.count(SETTINGS)

		create_project_settings.execute()
		create_project_settings.execute()

		copy = frappe.get_doc(SETTINGS, without.name)
		shared = [df.fieldname for df in _value_fields(SETTINGS) if df.fieldname in today_settings]
		self.assertTrue(len(shared) >= 25)
		for field in shared:
			self.assertEqual(copy.get(field), today_settings[field], field)
		self.assertEqual(frappe.db.count(SETTINGS), before + 1, "running twice made extra records")
		self.assertEqual(frappe.db.get_value(SETTINGS, hand_filled.name, "forfeiture_percentage"), 12.5)
		self.assertFalse(frappe.get_all(SETTINGS, {"land_acquisition": ["in", cancelled or ["-"]]}))
		self.assertEqual(
			frappe.get_single("LandMS Settings").as_dict(), today_settings, "LandMS Settings changed"
		)
