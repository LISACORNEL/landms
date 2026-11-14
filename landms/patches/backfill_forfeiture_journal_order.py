"""Fill the new Sales Order link on every forfeiture journal a plot order was closed or cancelled with."""

import re

import frappe

from landms.patches.custom_fields.create_custom_fields import execute as create_custom_fields

REMARK = "Sales Order % cancelled before Plot Contract was submitted%"
ORDER_IN_REMARK = re.compile(r"^Sales Order (\S+) cancelled before Plot Contract was submitted")


def execute():
	if not frappe.get_meta("Journal Entry").has_field("lms_sales_order"):
		create_custom_fields()
	filled, skipped = backfill()
	frappe.logger().info(
		f"backfill_forfeiture_journal_order: {filled} journals linked to their order, {skipped} skipped"
	)


def backfill():
	"""Link each forfeiture journal to the order that names it, else to the order its remark names; returns (filled, skipped)."""
	filled = skipped = 0
	for journal, order in _journals_without_order().items():
		if not order:
			# its order no longer exists: left for a person to look at
			frappe.logger().info(f"backfill_forfeiture_journal_order: {journal} names no order, skipped")
			skipped += 1
			continue
		frappe.db.set_value("Journal Entry", journal, "lms_sales_order", order, update_modified=False)
		filled += 1
	return filled, skipped


def _journals_without_order():
	"""Forfeiture journals with no order link yet, each with its order (None when that order is gone)."""
	unlinked = {"lms_sales_order": ["is", "not set"]}
	found = {}
	for journal, remark in frappe.get_all(
		"Journal Entry",
		filters={"user_remark": ["like", REMARK], **unlinked},
		fields=["name", "user_remark"],
		as_list=True,
	):
		named = ORDER_IN_REMARK.match(remark or "")
		found[journal] = named.group(1) if named else None
	# the order's own pointer wins over the remark
	for order, journal in frappe.get_all(
		"Sales Order",
		filters={"forfeiture_entry": ["is", "set"]},
		fields=["name", "forfeiture_entry"],
		as_list=True,
	):
		if frappe.db.exists("Journal Entry", {"name": journal, **unlinked}):
			found[journal] = order
	orders = set(
		frappe.get_all(
			"Sales Order", filters={"name": ["in", [o for o in found.values() if o]]}, pluck="name"
		)
	)
	return {journal: (order if order in orders else None) for journal, order in found.items()}
