"""Each project's ERPNext Project, made and kept in step by its setting record."""

import frappe
from frappe import _

SETTING = "LandMS Project Setting"


def ensure_erpnext_project(setting, project=None):
	"""Create the Project, or bring its name, status and cost centre in line."""
	project = project or frappe.get_doc("Land Acquisition", setting.land_acquisition)
	name = setting.erpnext_project or (
		project.amended_from and frappe.db.get_value(SETTING, project.amended_from, "erpnext_project")
	)
	if name and frappe.db.exists("Project", name):
		project_name, status, cost_center = frappe.db.get_value(
			"Project", name, ["project_name", "status", "cost_center"]
		)
		changes = {}
		if project_name != project.acquisition_name:
			_refuse_if_taken(project.acquisition_name, but_not=name)
			changes["project_name"] = project.acquisition_name
		if status == "Cancelled" and project.docstatus < 2:
			changes["status"] = "Open"  # an amendment reopens it
		if setting.cost_center and cost_center != setting.cost_center:
			changes["cost_center"] = setting.cost_center
		if changes:
			frappe.db.set_value("Project", name, changes)
	else:
		_refuse_if_taken(project.acquisition_name)
		erpnext_project = frappe.get_doc(
			{
				"doctype": "Project",
				"project_name": project.acquisition_name,
				"company": project.company,
				"expected_start_date": project.acquisition_date,
				"cost_center": setting.cost_center or None,
			}
		)
		# no end date: a land project runs for years
		erpnext_project.insert(ignore_permissions=True, ignore_mandatory=True)
		erpnext_project.add_comment("Comment", _("Created with project {0}.").format(project.name))
		name = erpnext_project.name
	if setting.erpnext_project != name:
		setting.db_set("erpnext_project", name, update_modified=False)


def cancel_erpnext_project(project):
	"""Mark it Cancelled; never delete it, postings may use it."""
	name = frappe.db.get_value(SETTING, project.name, "erpnext_project")
	if name and frappe.db.exists("Project", name):
		frappe.db.set_value("Project", name, "status", "Cancelled")


def delete_erpnext_project(setting):
	"""Delete it, or only cancel it when documents use it."""
	if not setting.erpnext_project or not frappe.db.exists("Project", setting.erpnext_project):
		return
	try:
		frappe.delete_doc("Project", setting.erpnext_project, ignore_permissions=True)
	except frappe.LinkExistsError:
		frappe.clear_last_message()
		frappe.db.set_value("Project", setting.erpnext_project, "status", "Cancelled")


def _refuse_if_taken(project_name, but_not=None):
	"""Project names are unique."""
	if frappe.db.exists("Project", {"project_name": project_name, "name": ["!=", but_not or ""]}):
		frappe.throw(
			_("An ERPNext Project named {0} already exists. Rename this project.").format(project_name)
		)
