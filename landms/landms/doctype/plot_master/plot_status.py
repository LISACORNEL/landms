"""The one place that changes a plot's status (PM-1).

Before PM-1, fourteen places in four files wrote Plot Master.status straight into the
database: nothing recorded who changed it or why, and the rules lived in each caller.
Now every change goes through set_plot_status(), which

  * checks the move against ALLOWED_INTO (the moves the app made before PM-1 — not one
    move that worked then is refused now; the four unchecked ones are marked and wait
    for an operator decision), and
  * leaves a plain-words note on the plot's timeline: from, to, who, why, and the
    document behind it.

Callers keep their own "only when the plot is X" checks through `only_from`.
"""

import frappe
from frappe import _
from frappe.utils import get_fullname, get_link_to_form

STATUSES = (
	"Available",
	"Pending Fee",
	"Pending Advance",
	"Reserved",
	"Ready for Handover",
	"Delivered",
	"Title Closed",
)
_ANY = frozenset(STATUSES)

# status -> the statuses a plot may come FROM to reach it (the app's moves before PM-1)
ALLOWED_INTO = {
	# application submitted
	"Pending Fee": frozenset({"Available"}),
	# application fee paid or waived, sales order submitted or reopened
	"Pending Advance": frozenset({"Available", "Pending Fee"}),
	# Unchecked before PM-1 (kept as they were, 5 Nov 2026; each waits for an operator decision):
	"Reserved": _ANY,  # contract submitted (and advance met: from Pending Advance)
	"Available": _ANY,  # contract cancelled or terminated (also application / order cancelled)
	"Ready for Handover": _ANY,  # handover cancelled (and contract fully paid: not from Delivered)
	"Delivered": _ANY,  # handover submitted (its validate wants Ready for Handover)
	"Title Closed": frozenset(),  # nothing sets it
}


def set_plot_status(plot, to_status, because, *, only_from=None, update_modified=True):
	"""Move `plot` to `to_status` and note it on the plot's timeline; return True if it moved.

	because: plain words naming the event and its document, e.g. "application PA-0001 was submitted".
	only_from: the caller's own condition — the plot moves only from these statuses, otherwise
	nothing happens (as before PM-1).
	"""
	if not plot:
		return False
	row = frappe.db.get_value("Plot Master", plot, ["name", "status"], as_dict=True)
	if not row:  # no such plot: the direct write used to change nothing, quietly
		return False
	current = row.status or ""
	if only_from is not None and current not in only_from:
		return False
	if to_status not in ALLOWED_INTO:
		frappe.throw(_("{0} is not a plot status.").format(to_status))
	# a plot with no status yet may take any (the direct write allowed it)
	if current and current != to_status and current not in ALLOWED_INTO[to_status]:
		frappe.throw(
			_("Plot {0} cannot move from {1} to {2} ({3}).").format(plot, current, to_status, because)
		)
	frappe.db.set_value("Plot Master", plot, "status", to_status, update_modified=update_modified)
	frappe.clear_document_cache("Plot Master", plot)
	if current != to_status:
		_note_on_timeline(plot, current, to_status, because)
		for listener in _listeners():
			listener(plot=plot, from_status=current, to_status=to_status)
	return True


def _listeners():
	"""Functions other apps list under the hook plot_status_changed, to follow a plot's status."""
	return [frappe.get_attr(path) for path in frappe.get_hooks("plot_status_changed")]


def link(doctype, name):
	"""A clickable link to a document, for the `because` text."""
	return get_link_to_form(doctype, name) if name else ""


def _note_on_timeline(plot, from_status, to_status, because):
	user = frappe.session.user
	who = get_fullname(user) if user not in ("Guest", "Administrator") else user
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Info",
			"reference_doctype": "Plot Master",
			"reference_name": plot,
			"content": f"Status: {from_status or 'no status'} → {to_status}, by {who}, because {because}.",
		}
	).insert(ignore_permissions=True)
