"""Stamp the project's ERPNext Project beside the Land Acquisition tag; never stops a save."""

import frappe
from frappe import _


def stamp_project(doc, method=None):
	"""validate hook on the document types LandMS tags."""
	try:
		replaced = _stamp(doc)
	except Exception:
		frappe.log_error(title=f"LandMS: project not stamped on {doc.doctype} {doc.name}")
		return
	if replaced:
		frappe.msgprint(
			_("Project set to {0}, as the Land Acquisition says.").format(", ".join(sorted(replaced))),
			indicator="orange",
			alert=True,
		)


def _stamp(doc):
	"""Stamp the document and its lines; return the projects that replaced another value."""
	meta = frappe.get_meta(doc.doctype)
	tag = doc.get("land_acquisition") if meta.has_field("land_acquisition") else None
	rows = [(doc, tag)] if tag and meta.has_field("project") else []
	for table in meta.get_table_fields():
		line_meta = frappe.get_meta(table.options)
		if not line_meta.has_field("project"):
			continue
		tagged_lines = line_meta.has_field("land_acquisition")
		for line in doc.get(table.fieldname) or []:
			line_tag = (line.get("land_acquisition") if tagged_lines else None) or tag
			if line_tag:
				rows.append((line, line_tag))

	projects, replaced = {}, set()
	for row, land_acquisition in rows:
		if land_acquisition not in projects:
			projects[land_acquisition] = frappe.db.get_value(
				"LandMS Project Setting", land_acquisition, "erpnext_project"
			)
		project = projects[land_acquisition]
		if project and row.get("project") != project:
			if row.get("project"):
				replaced.add(project)
			row.project = project
	return replaced
