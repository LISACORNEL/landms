"""The one place that changes a Plot Application's status."""

import frappe
from frappe import _
from frappe.model.document import Document

STATUSES = ("Draft", "Submitted", "Paid", "Converted", "Expired", "Cancelled")
_OPEN = frozenset({"Submitted", "Paid", "Converted"})

# status -> the statuses an application may come from to reach it
ALLOWED_INTO = {
	"Draft": frozenset(),  # only a new or amended draft has it
	"Submitted": frozenset({"Draft"}),  # submitted with a fee to pay
	"Paid": frozenset({"Draft", "Submitted", "Cancelled", "Expired"}),  # no fee, fee recorded, order reopened
	"Converted": frozenset({"Paid"}),  # the advance was paid
	"Expired": _OPEN,  # the night run ended it
	"Cancelled": _OPEN,  # cancelled by hand, with its order or with its contract
}


def set_application_status(application, to_status, because="", *, only_from=None, update_modified=True):
	"""Move an application (document or name) to a status and note it on its timeline; True if it moved."""
	doc = application if isinstance(application, Document) else None
	name = doc.name if doc else application
	if not name:
		return False
	row = frappe.db.get_value("Plot Application", name, ["name", "status"], as_dict=True)
	if not row:
		return False
	current = row.status or ""
	if only_from is not None and current not in only_from:
		return False
	if to_status not in ALLOWED_INTO:
		frappe.throw(_("{0} is not an application status.").format(to_status))
	# an application with no status yet may take any
	if current and current != to_status and current not in ALLOWED_INTO[to_status]:
		frappe.throw(_("Application {0} cannot move from {1} to {2}.").format(name, current, to_status))
	if doc:
		doc.db_set("status", to_status, update_modified=update_modified)
	else:
		frappe.db.set_value("Plot Application", name, "status", to_status, update_modified=update_modified)
		frappe.clear_document_cache("Plot Application", name)
	if current != to_status:
		_note_on_timeline(name, to_status, because)
	return True


def _note_on_timeline(name, to_status, because):
	"""A short history line on the application: the new status and, when given, why."""
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Info",
			"reference_doctype": "Plot Application",
			"reference_name": name,
			"content": f"{to_status}: {because}" if because else to_status,
		}
	).insert(ignore_permissions=True)
