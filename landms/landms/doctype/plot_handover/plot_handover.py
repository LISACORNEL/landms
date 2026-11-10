import frappe
from frappe.model.document import Document
from frappe.utils import flt, get_fullname, today

from landms.landms.doctype.land_acquisition.land_acquisition import sync_land_acquisition_plot_summary
from landms.landms.doctype.plot_master.plot_status import link, set_plot_status


class PlotHandover(Document):
	def before_validate(self):
		if self.docstatus == 0:
			# a draft never carries a verification code: Amend copies the
			# cancelled receipt's code (and the code must be unique), and a code
			# typed in by someone must never reach a receipt. Submit gives a new one.
			self.verification_code = None
		self._fill_from_contract()
		self._fill_company_representative()

	def validate(self):
		self._ensure_no_existing_handover()

	def before_submit(self):
		from landms.receipt_verification import assign_code, money_owed

		owed = money_owed(self.contract)
		if owed is None or owed > 0:
			frappe.throw(
				f"Contract {self.contract} is not fully paid. A plot is handed over only when nothing is owed."
			)
		assign_code(self)

	def on_submit(self):
		delivery_note = self._ensure_delivery_note()
		if delivery_note and self.delivery_note != delivery_note:
			self.db_set("delivery_note", delivery_note)
		set_plot_status(self.plot, "Delivered", f"handover {link('Plot Handover', self.name)} was submitted")
		self._sync_land_acquisition_summary()
		self.db_set("handover_status", "Completed")

	def on_cancel(self):
		self._cancel_delivery_note()
		set_plot_status(
			self.plot, "Ready for Handover", f"handover {link('Plot Handover', self.name)} was cancelled"
		)
		self._sync_land_acquisition_summary()
		self.db_set("handover_status", "Draft")

	def _ensure_no_existing_handover(self):
		if not self.contract:
			return

		existing = frappe.db.get_value(
			"Plot Handover",
			{
				"contract": self.contract,
				"docstatus": 1,
				"name": ("!=", self.name),
			},
			"name",
		)
		if existing:
			frappe.throw(f"Contract {self.contract} already has a submitted Plot Handover: {existing}.")

	def _fill_from_contract(self):
		"""Auto-fill plot and customer details from the linked Plot Contract."""
		if not self.contract:
			return

		contract = frappe.get_doc("Plot Contract", self.contract)

		if contract.contract_status != "Completed":
			frappe.throw(
				f"Contract {self.contract} is not Completed. "
				"A handover document can only be created for a fully paid contract."
			)

		plot_status = frappe.db.get_value("Plot Master", contract.plot, "status")
		if plot_status not in ("Ready for Handover", "Delivered"):
			frappe.throw(
				f"Plot {contract.plot} is in status {plot_status}. "
				"A handover can only be created when the plot is Ready for Handover."
			)

		self.customer = contract.customer
		self.plot = contract.plot
		self.acquisition_name = contract.acquisition_name
		self.land_acquisition = contract.land_acquisition
		self.contract_date = contract.contract_date
		self.selling_price = contract.selling_price

	def _fill_company_representative(self):
		defaults = get_logged_in_representative_details()
		if not self.handed_over_by and defaults.get("handed_over_by"):
			self.handed_over_by = defaults["handed_over_by"]
		if not self.handed_over_by_title and defaults.get("handed_over_by_title"):
			self.handed_over_by_title = defaults["handed_over_by_title"]

	def _ensure_delivery_note(self):
		if self.delivery_note and frappe.db.exists("Delivery Note", self.delivery_note):
			dn = frappe.get_doc("Delivery Note", self.delivery_note)
			if dn.docstatus == 1:
				return dn.name
			if dn.docstatus == 0:
				dn.submit()
				return dn.name

		contract = frappe.get_doc("Plot Contract", self.contract)
		plot = frappe.get_doc("Plot Master", self.plot)
		settings = frappe.get_single("LandMS Settings")

		if not contract.sales_order or not frappe.db.exists("Sales Order", contract.sales_order):
			frappe.throw(f"Contract {contract.name} has no Sales Order to deliver the plot from.")
		if not plot.serial_no:
			frappe.throw(f"Plot {plot.name} is missing its Serial No.")

		self._cancel_blocking_stock_reconciliations(plot.serial_no)

		dn = self._make_delivery_note_from_sales_order(contract.sales_order, plot)

		dn.run_method("set_missing_values")
		dn.run_method("calculate_taxes_and_totals")
		dn.run_method("set_use_serial_batch_fields")
		dn.posting_date = self.handover_date or today()
		dn.set_posting_time = 1
		dn.remarks = f"Plot handover {self.name} for contract {self.contract} / plot {self.plot}"

		# Override expense account and cost center on all items.
		# COGS must post to Cost of Land Sold; cost center from LandMS Settings.
		for item in dn.items:
			item.expense_account = settings.cogs_account
			item.cost_center = settings.cost_center

		dn.insert(ignore_permissions=True)
		dn.submit()
		frappe.db.commit()
		return dn.name

	def _make_delivery_note_from_sales_order(self, sales_order_name, plot):
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

		dn = make_delivery_note(sales_order_name)
		if not dn.items:
			frappe.throw(f"Sales Order {sales_order_name} has no deliverable rows for handover.")

		inv_warehouse = frappe.db.get_single_value("LandMS Settings", "plot_inventory_warehouse")
		for row in dn.items:
			row.qty = 1
			row.serial_no = plot.serial_no
			row.use_serial_batch_fields = 1
			row.warehouse = inv_warehouse
			row.incoming_rate = flt(plot.allocated_cost)

		return dn

	def _cancel_blocking_stock_reconciliations(self, serial_no):
		"""Ensure the serial number is Active and in the correct warehouse
		before creating the Delivery Note.

		Previous versions cancelled all Stock Reconciliations for the
		land acquisition to work around a serial-status bug.  That
		approach was destructive — cancelling an SR undoes the cost
		adjustment for ALL plots in that SR, not just the one being
		delivered.

		The root cause is that Stock Reconciliation's outward SLE marks
		serials as 'Delivered' and the inward SLE sometimes fails to
		restore them.  The correct fix is to simply reset the Serial No
		metadata (status and warehouse) so the DN validation passes.
		The SLE chain already shows the serial as available.
		"""
		if not serial_no:
			return

		inv_warehouse = frappe.db.get_single_value("LandMS Settings", "plot_inventory_warehouse")
		sn_data = frappe.db.get_value("Serial No", serial_no, ["status", "warehouse"], as_dict=True)
		if not sn_data:
			return

		if sn_data.status != "Active" or sn_data.warehouse != inv_warehouse:
			frappe.db.set_value(
				"Serial No",
				serial_no,
				{
					"status": "Active",
					"warehouse": inv_warehouse,
				},
				update_modified=False,
			)

	def _cancel_delivery_note(self):
		if not self.delivery_note or not frappe.db.exists("Delivery Note", self.delivery_note):
			return

		dn = frappe.get_doc("Delivery Note", self.delivery_note)
		if dn.docstatus == 1:
			dn.cancel()

	def _sync_land_acquisition_summary(self):
		if self.land_acquisition:
			sync_land_acquisition_plot_summary(self.land_acquisition)


@frappe.whitelist()
def get_logged_in_representative_details():
	user = frappe.session.user
	if not user or user == "Guest":
		return {}

	employee = frappe.db.get_value(
		"Employee",
		{"user_id": user, "status": "Active"},
		["employee_name", "designation"],
		as_dict=True,
	)

	return {
		"handed_over_by": (
			employee.employee_name if employee and employee.employee_name else get_fullname(user) or ""
		).strip(),
		"handed_over_by_title": (employee.designation if employee and employee.designation else "").strip(),
	}


@frappe.whitelist()
def get_plot_handover_defaults(contract: str):
	if not contract:
		return {}

	if not frappe.db.exists("Plot Contract", contract):
		frappe.throw(f"Plot Contract {contract} was not found.")

	contract_doc = frappe.get_doc("Plot Contract", contract)
	if contract_doc.contract_status != "Completed":
		frappe.throw(
			f"Contract {contract_doc.name} is not Completed. A handover can only be created for a fully paid contract."
		)

	plot_status = frappe.db.get_value("Plot Master", contract_doc.plot, "status")
	if plot_status not in ("Ready for Handover", "Delivered"):
		frappe.throw(
			f"Plot {contract_doc.plot} is in status {plot_status}. "
			"A handover can only be created when the plot is Ready for Handover."
		)

	return {
		"customer": contract_doc.customer,
		"plot": contract_doc.plot,
		"acquisition_name": contract_doc.acquisition_name,
		"land_acquisition": contract_doc.land_acquisition,
		"contract_date": contract_doc.contract_date,
		"selling_price": contract_doc.selling_price,
	}
