"""LandMS Project Settings is now LandMS Project Setting: "Settings" in a doctype name reads as a single doctype.

Runs before the doctypes load. A site that already has the old doctype (one that ran version-15-hotfix after
LA-7) keeps every record under the new name; a site that never had it (live) has nothing to do.
"""

import frappe

OLD, NEW = "LandMS Project Settings", "LandMS Project Setting"


def execute():
	if frappe.db.exists("DocType", OLD) and not frappe.db.exists("DocType", NEW):
		frappe.rename_doc("DocType", OLD, NEW, force=True)
