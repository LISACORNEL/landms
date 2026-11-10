"""One settings record per project (LA-7): what LandMS Settings holds, per project.

Left out, staying in LandMS Settings: the receipt verification address (one for the whole
system) and the TCB log naming series (it belongs to the channel).

Named after its project, so there is exactly one per project. A new project gets its record
empty and Finance fills it; the project cannot be approved while a field marked * is empty.
The government fees are informational (shown on plot documents, never posted). Projects that
existed before LA-7 get an exact copy of LandMS Settings (patch create_project_settings).
Not searchable and in no workspace: opened from the project's Connections. Code reads it only
through accounts_for(project).

LandMS Settings stays as it is: later cards move each reader to accounts_for, one place at a
time; card X-7 keeps the list of every place.
"""

import frappe
from frappe import _
from frappe.model import no_value_fields
from frappe.model.document import Document
from frappe.utils import get_link_to_form

SETTINGS = "LandMS Project Setting"
# required numbers that mean nothing at 0 (an empty number is stored as 0); the fee amount may be 0
NOT_ZERO = ("unpaid_application_expiry_days", "application_fee_validity_days", "forfeiture_percentage")
NUMBERS = ("Int", "Float", "Currency", "Percent")
OF_COMPANY = ("Account", "Warehouse", "Cost Center")


class LandMSProjectSetting(Document):
	def validate(self):
		self._check_company()
		self._warn_if_accounts_change_after_postings()

	def _check_company(self):
		"""Every account, warehouse and cost centre must belong to the project's company."""
		for df in self.meta.get_link_fields():
			value = self.get(df.fieldname)
			if df.options in OF_COMPANY and value:
				if frappe.db.get_value(df.options, value, "company") != self.company:
					frappe.throw(_("{0}: {1} belongs to another company.").format(_(df.label), value))

	def _warn_if_accounts_change_after_postings(self):
		"""Changing an account once the project has postings is allowed, but said out loud."""
		before = self.get_doc_before_save()
		if not before or not frappe.db.has_column("GL Entry", "land_acquisition"):
			return
		accounts = [df for df in self.meta.get_link_fields() if df.options == "Account"]
		changed = [_(df.label) for df in accounts if before.get(df.fieldname) != self.get(df.fieldname)]
		if changed and frappe.db.exists(
			"GL Entry", {"land_acquisition": self.land_acquisition, "is_cancelled": 0}
		):
			frappe.msgprint(
				_("This project already has postings. Changed: {0}.").format(", ".join(changed)),
				indicator="orange",
				alert=True,
			)


def copy_of_landms_settings(company):
	"""Today's LandMS Settings values for every field this record shares with it (same company only)."""
	today = frappe.get_single("LandMS Settings")
	if today.company != company:
		return {}
	shared = [
		df.fieldname
		for df in frappe.get_meta(SETTINGS).fields
		if df.fieldtype not in no_value_fields
		and df.fieldname != "company"
		and today.meta.has_field(df.fieldname)
	]
	return {field: today.get(field) for field in shared}


def missing_fields(settings):
	"""The labels of the fields marked * that are still empty (for NOT_ZERO numbers, 0 is empty)."""
	missing = []
	for df in settings.meta.get("fields", {"reqd": 1}):
		value = settings.get(df.fieldname)
		empty = (df.fieldname in NOT_ZERO and not value) if df.fieldtype in NUMBERS else not value
		if empty:
			missing.append(_(df.label))
	return missing


def accounts_for(land_acquisition):
	"""A project's settings: the one way code reads them."""
	if not frappe.db.exists(SETTINGS, land_acquisition):
		frappe.throw(_("Project {0} has no settings yet.").format(land_acquisition))
	return frappe.get_cached_doc(SETTINGS, land_acquisition)


def ensure_project_settings(doc, method=None, copy=None):
	"""Project hook (on_update): every project has its settings record, linked and named after it;
	a draft project's new name is carried over. Created empty for Finance to fill, or, from the
	patch, as an exact copy of LandMS Settings."""
	current = frappe.db.get_value(SETTINGS, doc.name, ["name", "project_name"], as_dict=True)
	if current:
		if current.project_name != doc.acquisition_name:
			frappe.db.set_value(SETTINGS, doc.name, "project_name", doc.acquisition_name)
		return
	record = frappe.get_doc(
		{
			"doctype": SETTINGS,
			"land_acquisition": doc.name,
			"project_name": doc.acquisition_name,
			"company": doc.company,
			**(copy or {}),
		}
	)
	# a copy is kept exactly as LandMS Settings has it
	record.flags.ignore_validate = bool(copy)
	record.insert(ignore_permissions=True, ignore_mandatory=True)


def check_project_settings_complete(doc, method=None):
	"""Project hook (before_submit): refuse while a field marked * is empty."""
	ensure_project_settings(doc)
	missing = missing_fields(frappe.get_doc(SETTINGS, doc.name))
	if missing:
		frappe.throw(
			_("Fill the project's settings first ({0}): {1}.").format(
				get_link_to_form(SETTINGS, doc.name), ", ".join(missing)
			)
		)


def delete_project_settings(doc, method=None):
	"""Project hook (on_trash): a deleted project takes its settings record with it."""
	if frappe.db.exists(SETTINGS, doc.name):
		frappe.delete_doc(SETTINGS, doc.name, ignore_permissions=True)
