"""LA-7: every project that is not cancelled gets its settings record, an exact copy of LandMS Settings.

Additive only: nothing is corrected here (the accountant's check, X-6, decides), an existing record
is never changed, and LandMS Settings itself is left untouched.
"""

import frappe

from landms.landms.doctype.landms_project_setting.landms_project_setting import (
	copy_of_landms_settings,
	ensure_project_settings,
)


def execute():
	for name in frappe.get_all(
		"Land Acquisition", {"docstatus": ["<", 2]}, pluck="name", order_by="creation"
	):
		project = frappe.get_doc("Land Acquisition", name)
		ensure_project_settings(project, copy=copy_of_landms_settings(project.company))
