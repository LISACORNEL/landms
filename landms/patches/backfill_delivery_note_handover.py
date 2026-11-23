"""Fill the new Plot Handover link on every delivery note a handover made."""

import frappe

from landms.patches.custom_fields.create_custom_fields import execute as create_custom_fields


def execute():
	if not frappe.get_meta("Delivery Note").has_field("plot_handover"):
		create_custom_fields()
	filled, skipped = backfill()
	frappe.logger().info(
		f"backfill_delivery_note_handover: {filled} delivery notes linked to their handover, {skipped} skipped"
	)


def backfill():
	"""Link each delivery note to its handover; returns (filled, skipped)."""
	filled = skipped = 0
	for note, handovers in _notes_without_handover().items():
		if len(handovers) != 1:
			# one note claimed by several handovers: left for a person to look at
			frappe.logger().info(
				f"backfill_delivery_note_handover: {note} is claimed by {len(handovers)} handovers, skipped"
			)
			skipped += 1
			continue
		frappe.db.set_value("Delivery Note", note, "plot_handover", handovers.pop(), update_modified=False)
		filled += 1
	return filled, skipped


def _notes_without_handover():
	"""Delivery notes with no handover link yet, each with the handovers that claim it."""
	claims = {}
	for handover, note in frappe.get_all(
		"Plot Handover",
		filters={"delivery_note": ["is", "set"], "docstatus": ["!=", 0]},
		fields=["name", "delivery_note"],
		as_list=True,
	):
		claims.setdefault(note, set()).add(handover)
	for note, handover in _notes_a_handover_forgot():
		claims.setdefault(note, set()).add(handover)
	unlinked = (
		set(
			frappe.get_all(
				"Delivery Note",
				filters={"name": ["in", list(claims)], "plot_handover": ["is", "not set"]},
				pluck="name",
			)
		)
		if claims
		else set()
	)
	return {note: handovers for note, handovers in claims.items() if note in unlinked}


def _notes_a_handover_forgot():
	"""A submitted handover that never stored its note: the submitted note for its plot and its order."""
	pairs = []
	for handover in frappe.get_all(
		"Plot Handover",
		filters={"docstatus": 1, "delivery_note": ["is", "not set"]},
		fields=["name", "plot", "contract"],
	):
		order = frappe.db.get_value("Plot Contract", handover.contract, "sales_order")
		if not (handover.plot and order):
			continue
		line = frappe.qb.DocType("Delivery Note Item")
		note = frappe.qb.DocType("Delivery Note")
		found = (
			frappe.qb.from_(line)
			.inner_join(note)
			.on(note.name == line.parent)
			.select(note.name)
			.distinct()
			.where(note.docstatus == 1)
			.where(line.serial_no == handover.plot)
			.where(line.against_sales_order == order)
			.run(pluck=True)
		)
		if len(found) == 1:
			pairs.append((found[0], handover.name))
	return pairs
