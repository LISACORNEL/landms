"""Give every live project's setting record its ERPNext Project (runs after create_project_settings)."""

import frappe

from landms.landms.doctype.landms_project_setting.erpnext_project import SETTING, ensure_erpnext_project


def execute():
	for name in frappe.get_all(
		SETTING, {"erpnext_project": ["is", "not set"]}, pluck="name", order_by="creation"
	):
		project = frappe.get_doc("Land Acquisition", name)
		if project.docstatus == 2:
			continue
		try:
			ensure_erpnext_project(frappe.get_doc(SETTING, name), project)
		except frappe.ValidationError:
			frappe.log_error(title=f"LandMS: no ERPNext Project for {name}")
