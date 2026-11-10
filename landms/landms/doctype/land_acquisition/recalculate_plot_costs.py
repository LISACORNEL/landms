"""The Recalculate Plot Costs button: revalue every unsold plot to the project's current land cost (LA-2).

System Manager only; the work runs as one background job. Moved unchanged from land_acquisition.py.
"""

import frappe
from frappe import _
from frappe.utils import create_batch, flt


@frappe.whitelist(methods=["POST"])
def recalculate_plot_costs(land_acquisition: str):
	"""Enqueue a background job to update plot stock valuations to the current LA cost.

	System Manager only: it rewrites the stock value of every unsold plot. A plain
	role check, not frappe.only_for, which is switched off in tests and so could
	never be proven.
	"""
	if "System Manager" not in frappe.get_roles():
		frappe.throw(_("Only a System Manager can recalculate plot costs."), frappe.PermissionError)
	la = frappe.db.get_value(
		"Land Acquisition",
		land_acquisition,
		["docstatus", "recalculation_in_progress"],
		as_dict=True,
	)
	if not la or la.docstatus != 1:
		frappe.throw("Land Acquisition must be submitted.")
	if la.recalculation_in_progress:
		frappe.throw("A recalculation is already running. Please wait for it to complete.")

	frappe.db.set_value(
		"Land Acquisition",
		land_acquisition,
		"recalculation_in_progress",
		1,
		update_modified=False,
	)
	frappe.db.commit()

	frappe.enqueue(
		"landms.landms.doctype.land_acquisition.recalculate_plot_costs._run_plot_cost_recalculation",
		land_acquisition=land_acquisition,
		queue="long",
		timeout=3600,
		job_name=f"recalc_plot_costs_{land_acquisition}",
		now=frappe.conf.get("developer_mode"),
	)
	return {"status": "queued"}


def _run_plot_cost_recalculation(land_acquisition):
	"""Background job: create Stock Reconciliations to adjust un-delivered
	plot valuations to match the current Land Acquisition cost.

	Uses ERPNext's Stock Reconciliation to properly create SLEs and GL
	entries rather than manipulating ledger records directly.  Stock
	Reconciliation has a known limitation where serialised items are
	temporarily marked as 'Delivered' during the outward SLE — we fix
	the Serial No status/warehouse metadata after each batch.
	"""
	from landms.landms.doctype.plot_master.plot_master import get_plot_item_code

	def _finish(log, cost_to_save=None):
		update = {
			"recalculation_in_progress": 0,
			"last_recalculation_date": frappe.utils.now(),
			"last_recalculation_log": log,
		}
		if cost_to_save is not None:
			update["last_recalculation_cost"] = cost_to_save
		frappe.db.set_value("Land Acquisition", land_acquisition, update, update_modified=False)
		frappe.db.commit()

	try:
		settings = frappe.get_single("LandMS Settings")
		lud_account = settings.land_under_development_account
		warehouse = settings.plot_inventory_warehouse

		if not lud_account:
			_finish("FAILED: Land Under Development Account not set in LandMS Settings.")
			return
		if not warehouse:
			_finish("FAILED: Plot Inventory Warehouse not set in LandMS Settings.")
			return

		la = frappe.db.get_value(
			"Land Acquisition",
			land_acquisition,
			[
				"company",
				"total_area_sqm",
				"acquisition_cost_tzs",
				"last_recalculation_cost",
				"last_recalculation_log",
			],
			as_dict=True,
		)

		if not flt(la.total_area_sqm):
			_finish("FAILED: Total Area (sqm) is not set on this Land Acquisition.")
			return

		new_total_cost = flt(la.acquisition_cost_tzs)
		if not new_total_cost:
			_finish("FAILED: Acquisition cost is zero. Submit Purchase Invoices first.")
			return

		# Guard — only skip if last run was clean (no failures recorded)
		prev_log = la.last_recalculation_log or ""
		prev_had_failures = "FAILED" in prev_log or "✗" in prev_log
		if flt(la.last_recalculation_cost) == new_total_cost and not prev_had_failures:
			_finish("Nothing to do — plot costs are already up to date.", cost_to_save=new_total_cost)
			return

		cost_per_sqm = new_total_cost / flt(la.total_area_sqm)

		plots = frappe.get_all(
			"Plot Master",
			filters={"land_acquisition": land_acquisition, "docstatus": 1},
			fields=[
				"name",
				"plot_type",
				"plot_size_sqm",
				"allocated_cost",
				"serial_no",
				"stock_entry",
				"status",
			],
		)

		updated = []
		skipped = []
		failures = []
		sr_items = []

		for plot in plots:
			if plot.status in ("Delivered", "Title Closed"):
				skipped.append(f"{plot.name} — already delivered (manual JE adjustment needed)")
				continue
			if not plot.stock_entry:
				skipped.append(f"{plot.name} — not in inventory (no stock entry)")
				continue
			if not plot.serial_no:
				skipped.append(f"{plot.name} — missing serial number")
				continue
			if not flt(plot.plot_size_sqm):
				skipped.append(f"{plot.name} — plot area is 0, cannot calculate cost")
				continue

			new_plot_cost = flt(cost_per_sqm) * flt(plot.plot_size_sqm)
			old_plot_cost = flt(plot.allocated_cost)

			if abs(new_plot_cost - old_plot_cost) < 0.01:
				# Allocated cost is already correct — but sync cost_per_sqm if stale
				if abs(flt(plot.cost_per_sqm) - flt(cost_per_sqm)) > 0.01:
					frappe.db.set_value(
						"Plot Master", plot.name, "cost_per_sqm", flt(cost_per_sqm), update_modified=False
					)
				skipped.append(f"{plot.name} — cost unchanged ({old_plot_cost:.2f})")
				continue

			try:
				item_code = get_plot_item_code(plot.plot_type)
			except Exception as e:
				failures.append(f"{plot.name} — item code error: {e}")
				continue

			sr_items.append(
				{
					"plot": plot,
					"item_code": item_code,
					"new_cost": new_plot_cost,
					"old_cost": old_plot_cost,
				}
			)

		if not sr_items:
			_finish(
				"Nothing to do — no plots need cost update.",
				cost_to_save=new_total_cost,
			)
			return

		# ── Adjust cost on each Stock Entry ────────────────────────────
		# Neither Stock Reconciliation (SerialNoWarehouseError) nor Landed
		# Cost Voucher (doesn't support Stock Entry as receipt type) works
		# for serialised items from Stock Entries.
		#
		# Instead, we replicate exactly what LCV's update_landed_cost()
		# does internally (lines 244-259 of landed_cost_voucher.py):
		#   1. Update item rates on the Stock Entry
		#   2. Cancel SLEs with via_landed_cost_voucher=True  (bypasses
		#      Serial and Batch Bundle validation)
		#   3. Resubmit SLEs with via_landed_cost_voucher=True
		#   4. Redo GL entries
		#   5. Repost future SLE/GLE
		#   6. Update Serial No purchase_rate
		BATCH_SIZE = 50
		adjusted_se = []

		for batch in create_batch(sr_items, BATCH_SIZE):
			for item in batch:
				try:
					doc = frappe.get_doc("Stock Entry", item["plot"].stock_entry)
					new_rate = flt(item["new_cost"])

					# ── Step 1: Update item rates on the Stock Entry ─────
					for se_item in doc.get("items"):
						if se_item.serial_no == item["plot"].serial_no or len(doc.items) == 1:
							se_item.basic_rate = new_rate
							se_item.basic_amount = flt(new_rate * se_item.qty)
							se_item.amount = se_item.basic_amount
							se_item.valuation_rate = new_rate
							se_item.db_update()
							break

					doc.total_incoming_value = sum(flt(d.amount) for d in doc.get("items") if d.t_warehouse)
					doc.value_difference = doc.total_incoming_value - doc.total_outgoing_value
					doc.total_amount = doc.total_incoming_value
					doc.db_update()

					# ── Step 2: Update Serial No purchase_rate ────────────
					frappe.db.set_value(
						"Serial No",
						item["plot"].serial_no,
						"purchase_rate",
						new_rate,
						update_modified=False,
					)

					# ── Step 3: Cancel SLEs (same as LCV line 247-249) ───
					# Stock Entry.update_stock_ledger() does NOT accept
					# via_landed_cost_voucher, so we inline its logic and
					# call make_sl_entries() directly with the flag.
					doc.docstatus = 2
					sl_entries = []
					finished_item_row = doc.get_finished_item_row()
					doc.get_sle_for_source_warehouse(sl_entries, finished_item_row)
					doc.get_sle_for_target_warehouse(sl_entries, finished_item_row)
					sl_entries.reverse()
					doc.make_sl_entries(
						sl_entries,
						allow_negative_stock=True,
						via_landed_cost_voucher=True,
					)
					doc.make_gl_entries_on_cancel()

					# ── Step 4: Resubmit SLEs (same as LCV line 252-259) ─
					doc.docstatus = 1
					doc.make_bundle_using_old_serial_batch_fields(
						via_landed_cost_voucher=True,
					)
					sl_entries = []
					doc.get_sle_for_source_warehouse(sl_entries, finished_item_row)
					doc.get_sle_for_target_warehouse(sl_entries, finished_item_row)
					doc.make_sl_entries(
						sl_entries,
						allow_negative_stock=True,
						via_landed_cost_voucher=True,
					)
					doc.make_gl_entries()
					doc.repost_future_sle_and_gle(via_landed_cost_voucher=True)

					# ── Step 5: Update Plot Master ────────────────────────
					frappe.db.set_value(
						"Plot Master",
						item["plot"].name,
						{
							"allocated_cost": item["new_cost"],
							"cost_per_sqm": flt(cost_per_sqm),
						},
						update_modified=False,
					)

					adjusted_se.append(item["plot"].stock_entry)
					updated.append(f"{item['plot'].name} ({item['old_cost']:.0f} → {item['new_cost']:.0f})")

				except Exception:
					frappe.log_error(
						title=f"Plot cost adjust failed: {item['plot'].name}",
						message=frappe.get_traceback(),
						reference_doctype="Land Acquisition",
						reference_name=land_acquisition,
					)
					failures.append(f"{item['plot'].name} — cost adjustment failed (see Error Log)")

			frappe.db.commit()

		# Build result log
		lines = [
			f"Run: {frappe.utils.now()}",
			f"Total cost: {new_total_cost:,.0f} TZS  |  Cost/sqm: {cost_per_sqm:,.2f} TZS",
			"",
			f"✓ Updated:  {len(updated)}",
		]
		if adjusted_se:
			lines.append(f"   Stock Entries adjusted: {len(set(adjusted_se))}")
		for item in updated:
			lines.append(f"   {item}")
		lines += ["", f"→ Skipped:  {len(skipped)}"]
		for item in skipped:
			lines.append(f"   {item}")
		if failures:
			lines += ["", f"✗ Failed:   {len(failures)}  ← re-run to retry"]
			for item in failures:
				lines.append(f"   {item}")

		log = "\n".join(lines)
		cost_to_save = new_total_cost if not failures else None
		_finish(log, cost_to_save=cost_to_save)

	except Exception:
		frappe.log_error(
			title=f"Plot cost recalculation crashed: {land_acquisition}",
			message=frappe.get_traceback(),
			reference_doctype="Land Acquisition",
			reference_name=land_acquisition,
		)
		_finish("FAILED (crash) — see Error Log for details.")
