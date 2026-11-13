"""Fill the new Plot link on every stock receipt that put a plot into stock."""

import frappe

from landms.patches.custom_fields.create_custom_fields import execute as create_custom_fields


def execute():
	if not frappe.get_meta("Stock Entry").has_field("plot"):
		create_custom_fields()
	filled, skipped = backfill()
	frappe.logger().info(
		f"backfill_stock_entry_plot: {filled} receipts linked to their plot, {skipped} skipped"
	)


def backfill():
	"""A plot's receipt carries the plot's name as its serial number; returns (filled, skipped)."""
	filled = skipped = 0
	for receipt, plots in _receipts_without_plot().items():
		if len(plots) != 1:
			# one receipt for several plots: left for a person to look at
			frappe.logger().info(f"backfill_stock_entry_plot: {receipt} names {len(plots)} plots, skipped")
			skipped += 1
			continue
		frappe.db.set_value("Stock Entry", receipt, "plot", plots.pop(), update_modified=False)
		filled += 1
	return filled, skipped


def _receipts_without_plot():
	"""Material Receipts with no Plot yet, each with the plots its lines name by serial number."""
	entry, line, plot = (frappe.qb.DocType(d) for d in ("Stock Entry", "Stock Entry Detail", "Plot Master"))
	rows = (
		frappe.qb.from_(entry)
		.inner_join(line)
		.on(line.parent == entry.name)
		.inner_join(plot)
		.on(plot.name == line.serial_no)
		.select(entry.name, plot.name)
		.where(entry.stock_entry_type == "Material Receipt")
		.where(entry.plot.isnull() | (entry.plot == ""))
		.run()
	)
	receipts = {}
	for receipt, plot_name in rows:
		receipts.setdefault(receipt, set()).add(plot_name)
	return receipts
