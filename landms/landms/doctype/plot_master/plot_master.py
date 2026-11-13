import frappe
from frappe.model.document import Document
from frappe.utils import add_days, cint, flt, getdate, today

from landms.after_submit import drop_rounding_noise_on_locked_fields
from landms.landms.doctype.land_acquisition.land_acquisition import (
	sync_land_acquisition_plot_summary,
)


class PlotMaster(Document):
	def validate(self):
		self.validate_land_acquisition()
		self.fill_acquisition_name()
		self.fill_sales_defaults()
		self.fill_location_coordinates()
		self.fill_financials()
		self.validate_coordinate_pair()
		self.validate_duplicate_plot_number()
		self.validate_selling_price()

	def validate_land_acquisition(self):
		"""Plot Master can only hang off an Approved or Subdivided LA."""
		if not self.land_acquisition:
			return
		status = frappe.db.get_value("Land Acquisition", self.land_acquisition, "status")
		if status not in ("Approved", "Subdivided"):
			frappe.throw(
				f"Land Acquisition {self.land_acquisition} is not ready for subdivision "
				f"(current status: {status}). Only Approved or Subdivided acquisitions can be used."
			)

	def fill_acquisition_name(self):
		if not self.land_acquisition:
			self.acquisition_name = ""
			return
		self.acquisition_name = (
			frappe.db.get_value("Land Acquisition", self.land_acquisition, "acquisition_name") or ""
		)

	def fill_sales_defaults(self):
		"""Snapshot the sales-default fields from the LA so they survive
		even if the LA is later edited."""
		if not self.land_acquisition:
			self.booking_fee_percent = 0
			self.government_share_percent = 0
			self.payment_completion_days = 0
			self.selling_price_per_sqm = 0
			return

		la = (
			frappe.db.get_value(
				"Land Acquisition",
				self.land_acquisition,
				["booking_fee_percent", "government_share_percent", "payment_completion_days"],
				as_dict=True,
			)
			or {}
		)

		self.booking_fee_percent = flt(la.get("booking_fee_percent"))
		self.government_share_percent = flt(la.get("government_share_percent"))
		self.payment_completion_days = int(la.get("payment_completion_days") or 0)
		self.selling_price_per_sqm = get_plot_type_selling_rate(self.land_acquisition, self.plot_type)

	def fill_location_coordinates(self):
		"""If the plot has no coordinates, fall back to the LA's coordinates."""
		if not self.land_acquisition:
			return

		coords = (
			frappe.db.get_value(
				"Land Acquisition",
				self.land_acquisition,
				["latitude", "longitude"],
				as_dict=True,
			)
			or {}
		)

		if self.latitude in (None, "", 0) and coords.get("latitude") not in (None, "", 0):
			self.latitude = flt(coords.get("latitude"))
		if self.longitude in (None, "", 0) and coords.get("longitude") not in (None, "", 0):
			self.longitude = flt(coords.get("longitude"))

	def fill_financials(self):
		"""Cost is sourced from the LA's per-sqm cost; selling price from the
		per-plot-type rate. Both are recomputed every save in case the LA is
		edited (snapshotting only happens for the cosmetic snapshot fields)."""
		if not self.land_acquisition:
			self.cost_per_sqm = 0
			self.allocated_cost = 0
			self.selling_price = 0
			return

		la = frappe.get_doc("Land Acquisition", self.land_acquisition)
		total_sqm = flt(la.total_area_sqm)
		plot_sqm = flt(self.plot_size_sqm)

		self.cost_per_sqm = 0
		self.allocated_cost = 0

		if flt(la.acquisition_cost_tzs) > 0 and total_sqm > 0:
			self.cost_per_sqm = flt(la.get("cost_per_sqm_tzs")) or (flt(la.acquisition_cost_tzs) / total_sqm)
			if plot_sqm > 0:
				self.allocated_cost = self.cost_per_sqm * plot_sqm

		if flt(self.selling_price_per_sqm) > 0 and plot_sqm > 0:
			self.selling_price = flt(self.selling_price_per_sqm) * plot_sqm
		else:
			self.selling_price = 0

		self._fill_govt_fees(plot_sqm)

	def _fill_govt_fees(self, plot_sqm):
		land_rent_rate = flt(frappe.db.get_value("Plot Type", self.plot_type, "land_rent_rate"))
		settings = frappe.get_single("LandMS Settings")
		appl = flt(settings.govt_appl_fee_amount) or 5000
		reg_fee = flt(settings.govt_registration_fee_amount) or 25000
		prem_rate = flt(settings.govt_premium_rate) or 0.0025

		self.land_rent_rate = land_rent_rate
		self.land_rent = flt(land_rent_rate * plot_sqm)
		self.govt_application_fee = appl
		self.premium = flt(prem_rate * flt(self.selling_price))
		self.registration_prep_fee = flt(0.2 * self.land_rent)
		self.govt_registration_fee = reg_fee
		self.stamp_duty = flt(((self.land_rent - 2000) / 20) + 500) if self.land_rent > 0 else 0
		self.total_govt_fees = flt(
			self.govt_application_fee
			+ self.premium
			+ self.land_rent
			+ self.registration_prep_fee
			+ self.govt_registration_fee
			+ self.stamp_duty
		)
		self.total_plot_amount = flt(self.selling_price) + self.total_govt_fees

	def validate_coordinate_pair(self):
		"""Either both lat/lon are set, or both are blank."""
		has_lat = self.latitude not in (None, "", 0)
		has_lon = self.longitude not in (None, "", 0)
		if has_lat != has_lon:
			frappe.throw("Latitude and Longitude must be provided together.")

	def validate_duplicate_plot_number(self):
		if not self.plot_number or not self.land_acquisition:
			return
		existing = frappe.db.get_value(
			"Plot Master",
			{
				"land_acquisition": self.land_acquisition,
				"plot_number": self.plot_number,
				"name": ("!=", self.name),
				"docstatus": ("!=", 2),
			},
			"name",
		)
		if existing:
			frappe.throw(
				f"Plot number '{self.plot_number}' already exists for "
				f"Land Acquisition {self.land_acquisition} (see {existing})."
			)

	def validate_selling_price(self):
		if flt(self.selling_price_per_sqm) <= 0:
			frappe.throw(
				f"No selling rate found for plot type '{self.plot_type}' on "
				f"Land Acquisition {self.land_acquisition}. "
				"Add a row for this plot type in the Plot Type Rates table on the Land Acquisition."
			)
		if flt(self.selling_price) <= 0:
			frappe.throw("Selling Price must be greater than zero.")

	def on_submit(self):
		self.create_stock_entry()
		sync_land_acquisition_plot_summary(self.land_acquisition)

	# ------------------------------------------------------------------ #
	#  The 90-day tick (No Advance Deadline) on a submitted plot           #
	# ------------------------------------------------------------------ #

	def before_update_after_submit(self):
		# The form can hold the plot's money fields with more decimals than are
		# stored, which made Frappe refuse the save (live, 13 Oct 2026).
		drop_rounding_noise_on_locked_fields(self)
		stored_tick = cint(frappe.db.get_value("Plot Master", self.name, "no_advance_deadline"))
		self.flags.tick_changed = stored_tick != cint(self.no_advance_deadline)
		if self.flags.tick_changed and not cint(self.no_advance_deadline):
			self._refuse_untick_that_would_expire_an_order()

	def on_update_after_submit(self):
		if self.flags.tick_changed:
			self._update_live_application_windows()

	def _refuse_untick_that_would_expire_an_order(self):
		"""Without the tick a paid application gets the standard advance window
		again. If that window is over, tonight's run would expire the order at
		once — refuse instead (the same check the console script made).

		The run starts at 01:00, already on the next day, so a window ending
		today is also over by then: hence <= (the run expires window_end < its
		day, i.e. window_end <= today)."""
		standard = _standard_advance_days()
		for app in _paid_applications_on(self.name):
			window_end = add_days(app.payment_date, standard)
			if getdate(window_end) <= getdate(today()) and not _has_running_contract(app.sales_order):
				frappe.throw(
					f"The 90-day tick cannot be removed from {self.plot_number or self.name}: application "
					f"{app.name}'s standard {standard}-day advance window ran until {window_end}, so "
					"tonight's run would expire its order. Nothing was changed."
				)

	def _update_live_application_windows(self):
		"""Live applications on this plot follow the tick: validity days and the
		expiry date shown on the application, as when the application was made."""
		days = _advance_days_for(self)
		for app in _live_applications_on(self.name):
			values = {"validity_days": days}
			if app.payment_date:
				values["expiry_date"] = add_days(app.payment_date, days)
			frappe.db.set_value("Plot Application", app.name, values, update_modified=False)

	def on_cancel(self):
		self.cancel_stock_entry()
		sync_land_acquisition_plot_summary(self.land_acquisition)

	def create_stock_entry(self):
		"""Create a Material Receipt Stock Entry that moves `allocated_cost`
		from the Land Under Development account into the Plot Inventory
		warehouse, against a freshly minted Serial No tied to this plot."""
		settings = frappe.get_single("LandMS Settings")

		item_code = get_plot_item_code(self.plot_type)

		warehouse = settings.plot_inventory_warehouse
		if not warehouse:
			frappe.throw("Plot Inventory Warehouse is not set in LandMS Settings.")

		land_account = settings.land_under_development_account
		if not land_account:
			frappe.throw("Land Under Development Account is not set in LandMS Settings.")

		# Use the plot name (e.g. PLT-2026-0001) as the serial number — globally
		# unique and lets the plot ↔ serial ↔ stock-entry chain be traced by name.
		serial_number = self.name

		# Pre-create the Serial No record. ERPNext's validate_serialized_batch()
		# runs on Stock Entry insert (i.e. *before* on_submit), so the Serial No
		# must already exist by the time we hand it to the SE.
		if not frappe.db.exists("Serial No", serial_number):
			frappe.get_doc(
				{
					"doctype": "Serial No",
					"serial_no": serial_number,
					"item_code": item_code,
					"company": settings.company,
				}
			).insert(ignore_permissions=True)

		se = frappe.get_doc(
			_build_plot_stock_entry_doc(
				company=settings.company,
				plot_number=self.plot_number,
				land_acquisition=self.land_acquisition,
				item_code=item_code,
				basic_rate=flt(self.allocated_cost),
				target_warehouse=warehouse,
				serial_number=serial_number,
				difference_account=land_account,
				plot=self.name,
			)
		)
		se.insert(ignore_permissions=True)
		se.submit()

		self.db_set("stock_entry", se.name)
		self.db_set("serial_no", serial_number)

		frappe.msgprint(
			f"Plot entered inventory. Stock Entry: {se.name} | Serial No: {serial_number}",
			indicator="green",
			alert=True,
		)

	def cancel_stock_entry(self):
		if not self.stock_entry:
			return
		se = frappe.get_doc("Stock Entry", self.stock_entry)
		if se.docstatus == 1:
			se.cancel()
		self.db_set("stock_entry", None)
		self.db_set("serial_no", None)


def _standard_advance_days():
	return cint(frappe.db.get_single_value("LandMS Settings", "application_fee_validity_days") or 7)


def _advance_days_for(plot):
	"""The advance window a plot's applications get — the same rule as
	PlotApplication.fill_fee_from_settings and the nightly expiry."""
	if cint(plot.no_advance_deadline) and cint(plot.payment_completion_days) > 0:
		return cint(plot.payment_completion_days)
	return _standard_advance_days()


def _paid_applications_on(plot_name):
	return frappe.get_all(
		"Plot Application",
		filters={"plot": plot_name, "docstatus": 1, "status": "Paid", "payment_date": ["is", "set"]},
		fields=["name", "payment_date", "sales_order"],
	)


def _live_applications_on(plot_name):
	return frappe.get_all(
		"Plot Application",
		filters={"plot": plot_name, "docstatus": 1, "status": ["in", ["Submitted", "Paid"]]},
		fields=["name", "payment_date"],
	)


def _has_running_contract(sales_order):
	return bool(sales_order) and bool(
		frappe.db.exists("Plot Contract", {"sales_order": sales_order, "docstatus": 1})
	)


def get_plot_item_code(plot_type):
	"""Return the stock item code for the given plot type from the Plot Type doctype."""
	if not plot_type:
		frappe.throw("Plot Type is required.")
	item_code = frappe.db.get_value("Plot Type", plot_type, "item_code")
	if not item_code:
		frappe.throw(
			f"No stock item is set for plot type '{plot_type}'. "
			"Go to Plot Type → open the record and set the Stock Item."
		)
	return item_code


def get_plot_type_selling_rate(land_acquisition, plot_type):
	"""Look up the per-sqm selling rate for the given plot type from the LA's rate table."""
	if not land_acquisition or not plot_type:
		return 0
	result = frappe.db.get_value(
		"Land Acquisition Plot Type Rate",
		{"parent": land_acquisition, "plot_type": plot_type},
		"selling_price_per_sqm",
	)
	return flt(result)


def _build_plot_stock_entry_doc(
	*,
	company: str,
	plot_number: str,
	land_acquisition: str,
	item_code: str,
	basic_rate: float,
	target_warehouse: str,
	serial_number: str,
	difference_account: str,
	plot: str,
) -> dict:
	"""Build the Stock Entry payload used when a plot enters inventory.

	The plot items use a delivery-side item default expense account (COGS) for
	handover. Plot creation must not inherit that value; it needs to post against
	the Land Under Development asset account instead.
	"""
	return {
		"doctype": "Stock Entry",
		"stock_entry_type": "Material Receipt",
		"posting_date": frappe.utils.today(),
		"company": company,
		"remarks": f"Plot {plot_number} from {land_acquisition}",
		"land_acquisition": land_acquisition,
		# the receipt points back at its plot; the plot shows it under Connections
		"plot": plot,
		"difference_account": difference_account,
		"items": [
			{
				"item_code": item_code,
				"qty": 1,
				"basic_rate": flt(basic_rate),
				"t_warehouse": target_warehouse,
				"serial_no": serial_number,
				"use_serial_batch_fields": 1,
				# Prevent Stock Entry from inheriting the item default COGS account.
				"expense_account": difference_account,
			}
		],
	}


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_plot_types_for_la(doctype, txt, searchfield, start, page_len, filters):
	"""Plot Type dropdown on a plot: the types the plot's project has a selling rate for."""
	la = filters.get("land_acquisition") if isinstance(filters, dict) else None
	if not la:
		return []
	rate = frappe.qb.DocType("Land Acquisition Plot Type Rate")
	return (
		frappe.qb.from_(rate)
		.select(rate.plot_type)
		.distinct()
		.where((rate.parent == la) & rate.plot_type.like(f"%{txt}%"))
		.orderby(rate.plot_type)
		.limit(page_len)
		.offset(start)
		.run()
	)
