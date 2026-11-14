import frappe
from frappe.model.document import Document
from frappe.utils import add_days, cint, flt, getdate, today

from landms.landms.doctype.land_acquisition.land_acquisition import (
	sync_land_acquisition_plot_summary,
)
from landms.landms.doctype.plot_application.application_status import set_application_status
from landms.landms.doctype.plot_master.plot_status import link, set_plot_status
from landms.sales_order_hooks import build_payment_schedule_rows, build_sales_order_item_row


class PlotApplication(Document):
	# ------------------------------------------------------------------ #
	#  Validate / Submit / Cancel                                          #
	# ------------------------------------------------------------------ #

	def validate(self):
		self.reset_amended_application()
		self.validate_plot_available()
		self.fill_plot_details()
		self.fill_fee_from_settings()

	def before_submit(self):
		self._lock_plot_row()
		self._ensure_no_other_active_application_for_submit()

	def before_cancel(self):
		if self.status == "Converted" and not self.flags.get("_cancellation_reason"):
			frappe.throw(
				"This application has already been converted into an active sale. "
				"Cancel the Sales Order before first payment, or terminate the Plot Contract afterwards."
			)

	def on_submit(self):
		if flt(self.application_fee) <= 0:
			# Fee is waived — fast-forward to the same end-state the paid flow
			# produces (status=Paid, expiry stamped, plot Pending Advance, SO
			# auto-created) so the user doesn't need to record a dummy fee PE.
			payment_date = today()
			set_application_status(self, "Paid", "no fee")
			self.db_set("payment_date", payment_date)
			self.db_set("expiry_date", add_days(payment_date, int(self.validity_days or 7)))
			if set_plot_status(
				self.plot,
				"Pending Advance",
				f"application {link('Plot Application', self.name)} was submitted with no fee to pay",
				only_from=("Available",),
			):
				self._sync_land_acquisition_summary()
			self.create_sales_order(notify=0)
			return

		set_application_status(self, "Submitted")
		if not self.plot:
			return
		if set_plot_status(
			self.plot,
			"Pending Fee",
			f"application {link('Plot Application', self.name)} was submitted",
			only_from=("Available",),
		):
			self._sync_land_acquisition_summary()

	def on_cancel(self):
		"""Release the plot and set the correct terminal status.

		The scheduler passes doc.flags._cancellation_reason = "Expired" for
		paid-but-never-converted applications that blew past their deadline;
		everything else (manual cancel, unpaid timeout) becomes Cancelled.

		Contract termination passes _cancellation_reason = "Contract Terminated"
		— plot and SO are already handled by the contract, so skip those steps.
		"""
		reason = getattr(self.flags, "_cancellation_reason", None)
		from_termination = reason == "Contract Terminated"

		if not from_termination and self.status in ("Submitted", "Paid"):
			ended = "expired" if reason == "Expired" else "was cancelled"
			if set_plot_status(
				self.plot,
				"Available",
				f"application {link('Plot Application', self.name)} {ended}",
				only_from=("Pending Fee", "Pending Advance", "Reserved"),
			):
				self._sync_land_acquisition_summary()

		if not from_termination and self.status == "Paid":
			self._cancel_linked_sales_order_if_safe()

		if reason == "Expired":
			set_application_status(self, "Expired")
		else:
			set_application_status(self, "Cancelled", _why_cancelled(self, reason))

	# ------------------------------------------------------------------ #
	#  Validation helpers                                                  #
	# ------------------------------------------------------------------ #

	def reset_amended_application(self):
		"""Turn an amended application back into a true fresh draft.

		Frappe's amend flow copies most fields from the cancelled source doc,
		including old payment links and terminal statuses. For Plot Application
		we want the amended record to behave like a brand-new application for
		the same plot/customer.
		"""
		if not self.amended_from or self.docstatus != 0:
			return

		self.status = "Draft"
		self.application_date = today()
		self.payment_date = None
		self.reference_no = ""
		self.expiry_date = None
		self.sales_invoice = ""
		self.payment_entry = ""
		self.sales_order = ""

	def validate_plot_available(self):
		"""Draft-time checks: plot must be submitted and Available."""
		if not self.plot or self.docstatus != 0:
			return
		plot_doc = frappe.db.get_value("Plot Master", self.plot, ["docstatus", "status"], as_dict=True)
		if not plot_doc or plot_doc.docstatus != 1:
			frappe.throw(
				f"Plot {self.plot} has not been submitted yet. "
				"Submit the Plot Master before creating an application."
			)
		if plot_doc.status != "Available":
			frappe.throw(
				f"Plot {self.plot} is not Available (current status: {plot_doc.status}). "
				"Only Available plots can be applied for."
			)
		active = self._get_other_active_application(("Submitted", "Paid", "Converted"))
		if active:
			frappe.throw(
				f"Plot {self.plot} already has an active application ({active.name}, "
				f"status: {active.status}). That application must expire or be cancelled first."
			)

	def fill_plot_details(self):
		if not self.plot:
			self.land_acquisition = ""
			self.acquisition_name = ""
			return
		la = frappe.db.get_value("Plot Master", self.plot, "land_acquisition")
		if la:
			self.land_acquisition = la
			self.acquisition_name = frappe.db.get_value("Land Acquisition", la, "acquisition_name") or ""

	def fill_fee_from_settings(self):
		settings = frappe.get_single("LandMS Settings")
		self.application_fee = flt(settings.application_fee_amount)
		self.unpaid_validity_days = int(settings.unpaid_application_expiry_days or 3)
		self.validity_days = int(settings.application_fee_validity_days or 7)
		# Per-plot rule: a plot flagged "No Advance Deadline" grants the FULL
		# payment window (Payment Completion Days, defined on the Land Acquisition
		# and snapshotted onto the plot) instead of the standard advance window.
		if self.plot:
			plot_rule = frappe.db.get_value(
				"Plot Master",
				self.plot,
				["no_advance_deadline", "payment_completion_days"],
				as_dict=True,
			)
			if (
				plot_rule
				and cint(plot_rule.no_advance_deadline)
				and cint(plot_rule.payment_completion_days) > 0
			):
				self.validity_days = cint(plot_rule.payment_completion_days)

	def _lock_plot_row(self):
		"""Serialize submit/payment operations per plot to reduce race conditions."""
		if self.plot:
			frappe.db.sql(
				"select name from `tabPlot Master` where name=%s for update",
				(self.plot,),
			)

	def _get_other_active_application(self, statuses):
		return frappe.db.get_value(
			"Plot Application",
			{
				"plot": self.plot,
				"docstatus": 1,
				"status": ["in", list(statuses)],
				"name": ("!=", self.name),
			},
			["name", "status"],
			as_dict=True,
		)

	def _ensure_no_other_active_application_for_submit(self):
		if not self.plot:
			return

		plot_status = frappe.db.get_value("Plot Master", self.plot, "status")
		if plot_status != "Available":
			frappe.throw(
				f"Cannot submit application for plot {self.plot}. "
				f"Plot status is {plot_status}, expected Available."
			)

		active = self._get_other_active_application(("Submitted", "Paid", "Converted"))
		if active:
			frappe.throw(
				f"Cannot submit {self.name}. Plot {self.plot} already has active "
				f"application {active.name} (status: {active.status})."
			)

	def _sync_land_acquisition_summary(self):
		la = frappe.db.get_value("Plot Master", self.plot, "land_acquisition")
		if la:
			sync_land_acquisition_plot_summary(la)

	def _cancel_linked_sales_order_if_safe(self):
		"""Tear down the linked Sales Order if the application is being cancelled.

		Rules:
		  - draft SO: delete outright
		  - submitted SO: leave it alone if any advance has been received
		    (partial/full payment on the plot SI) — the Plot Contract owns it now
		  - submitted SO with no payment yet: cancel it
		"""
		if self.flags.get("from_sales_order_cancel"):
			# SO is already cancelling this PA — don't call back into SO.cancel().
			return
		if not self.sales_order or not frappe.db.exists("Sales Order", self.sales_order):
			return

		so = frappe.get_doc("Sales Order", self.sales_order)
		if so.docstatus == 0:
			# Break the self-referential link first so Frappe does not block
			# draft SO deletion while this Plot Application is being cancelled.
			frappe.db.set_value("Plot Application", self.name, "sales_order", "", update_modified=False)
			self.sales_order = ""
			frappe.delete_doc("Sales Order", so.name, ignore_permissions=True)
			return

		# Phase 5 will surface a `plot_sales_invoice` link on SO; until then
		# this branch is defensive — if no SI exists we fall through to cancel.
		plot_invoice = so.get("plot_sales_invoice")
		if plot_invoice and frappe.db.exists("Sales Invoice", plot_invoice):
			outstanding, grand_total = frappe.db.get_value(
				"Sales Invoice",
				plot_invoice,
				["outstanding_amount", "grand_total"],
			)
			if flt(outstanding) < flt(grand_total):
				return  # payment already happened — hands off

		so.cancel()
		self.db_set("sales_order", "")

	# ------------------------------------------------------------------ #
	#  Record Application Fee Payment                                      #
	# ------------------------------------------------------------------ #

	@frappe.whitelist()
	def record_fee_payment(self, payment_date, bank_account=None, reference_no=None):
		"""Record the application fee as an SI + PE pair.

		Accounting:
		  SI submit → Dr Accounts Receivable / Cr Application Fee Income
		  PE submit → Dr Bank/Cash       / Cr Accounts Receivable

		After a successful call:
		  - Plot moves from Pending Fee → Pending Advance
		  - Application moves from Submitted → Paid
		  - expiry_date = payment_date + validity_days
		  - Application fee SI is fully settled
		"""
		if self.status != "Submitted":
			frappe.throw("Application fee can only be recorded on a Submitted application.")
		if self.sales_invoice:
			frappe.throw("Application fee has already been recorded.")

		# Re-check under lock so only one submitted app can proceed to paid.
		self._lock_plot_row()
		active_paid = self._get_other_active_application(("Paid", "Converted"))
		if active_paid:
			frappe.throw(
				f"Cannot record payment for {self.name}. Plot {self.plot} is already "
				f"reserved by application {active_paid.name} (status: {active_paid.status})."
			)

		plot_status = frappe.db.get_value("Plot Master", self.plot, "status")
		if plot_status not in ("Available", "Pending Fee"):
			frappe.throw(
				f"Cannot record payment for {self.name}. Plot {self.plot} is {plot_status}, "
				"not ready for application fee payment."
			)

		settings = frappe.get_single("LandMS Settings")
		bank_account = bank_account or settings.application_fee_receiving_account
		if not bank_account:
			frappe.throw(
				"Application Fee Receiving Account is not configured in LandMS Settings. "
				"Set it first, or select a Bank/Cash account while recording payment."
			)
		fee_amount = flt(self.application_fee)
		if fee_amount <= 0:
			frappe.throw("Application fee amount must be greater than zero.")
		self._validate_receiving_account(bank_account, settings.company)

		# --- Sales Invoice ---
		if not settings.application_fee_item:
			frappe.throw("Application Fee Item is not configured in LandMS Settings.")

		si = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": self.customer,
				"posting_date": payment_date,
				"due_date": payment_date,
				"company": settings.company,
				"remarks": f"Application fee for Plot {self.plot} — Application {self.name}",
				"items": [
					{
						"item_code": settings.application_fee_item,
						"qty": 1,
						"rate": fee_amount,
						"income_account": settings.application_fee_income_account,
					}
				],
			}
		)
		si.insert(ignore_permissions=True)
		si.submit()

		# --- Payment Entry (settles the SI immediately) ---
		pe = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Receive",
				"posting_date": payment_date,
				"company": settings.company,
				"party_type": "Customer",
				"party": self.customer,
				"paid_from": si.debit_to,
				"paid_to": bank_account,
				"paid_amount": fee_amount,
				"received_amount": fee_amount,
				"reference_no": reference_no or self.name,
				"reference_date": payment_date,
				"remarks": f"Plot Application Fee Payment — {self.name} / Plot {self.plot}",
				"references": [
					{
						"reference_doctype": "Sales Invoice",
						"reference_name": si.name,
						"allocated_amount": fee_amount,
					}
				],
			}
		)
		pe.insert(ignore_permissions=True)
		pe.submit()

		expiry = add_days(payment_date, int(self.validity_days or 7))

		self.db_set("sales_invoice", si.name)
		self.db_set("payment_entry", pe.name)
		self.db_set("payment_date", payment_date)
		self.db_set("reference_no", reference_no or "")
		self.db_set("expiry_date", expiry)
		set_application_status(self, "Paid", "fee recorded")

		# Lock the plot until first advance is received or the paid window expires.
		set_plot_status(
			self.plot,
			"Pending Advance",
			f"the application fee for {link('Plot Application', self.name)} was recorded",
		)
		self._sync_land_acquisition_summary()

		so_name = self.create_sales_order(notify=0)

		frappe.msgprint(
			f"Application fee of TZS {fee_amount:,.0f} recorded. "
			f"Sales Order <b>{so_name}</b> created for Plot {self.plot}. "
			f"Reservation valid until {expiry}.",
			indicator="green",
			alert=True,
		)
		return si.name

	def _validate_receiving_account(self, account, company):
		info = frappe.db.get_value(
			"Account",
			account,
			["name", "company", "account_type", "is_group"],
			as_dict=True,
		)
		if not info:
			frappe.throw(f"Receiving account {account} was not found.")
		if cint(info.is_group):
			frappe.throw(f"{account} is a group account. Please choose a posting account.")
		if info.account_type not in ("Bank", "Cash"):
			frappe.throw(f"{account} is not a Bank/Cash account.")
		if info.company and info.company != company:
			frappe.throw(f"Receiving account {account} belongs to company {info.company}, not {company}.")

	# ------------------------------------------------------------------ #
	#  Create ERP Sales Order                                              #
	# ------------------------------------------------------------------ #

	@frappe.whitelist()
	def create_sales_order(self, notify=1):
		"""Create a draft ERP Sales Order from a Paid application.

		The SO is created in Draft so the user can review before submitting.
		Phase 5 will add SO hooks that drive the plot/status transitions.
		"""
		notify = cint(notify)

		if self.status != "Paid":
			frappe.throw("A Sales Order can only be created from a Paid application.")

		if self.sales_order and frappe.db.exists("Sales Order", self.sales_order):
			frappe.throw(f"A Sales Order has already been created: {self.sales_order}")

		# Clean stale link if it points to a missing SO.
		if self.sales_order and not frappe.db.exists("Sales Order", self.sales_order):
			self.db_set("sales_order", "")

		if self.expiry_date and getdate(self.expiry_date) < getdate(today()):
			frappe.throw("This application has expired. The plot reservation is no longer valid.")

		settings = frappe.get_single("LandMS Settings")
		plot = frappe.get_doc("Plot Master", self.plot)

		payment_completion_days = cint(plot.payment_completion_days or 0)
		if payment_completion_days <= 0:
			frappe.throw(f"Plot {plot.name} is missing Payment Completion Days.")

		transaction_date = self.payment_date or today()
		payment_deadline = add_days(transaction_date, payment_completion_days)

		item_row = build_sales_order_item_row(plot, settings.plot_inventory_warehouse, payment_deadline)
		payment_schedule_rows = build_payment_schedule_rows(
			total_amount=flt(plot.selling_price),
			booking_fee_percent=flt(plot.booking_fee_percent),
			transaction_date=transaction_date,
			payment_deadline=payment_deadline,
		)

		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"company": settings.company,
				"customer": self.customer,
				"transaction_date": transaction_date,
				"delivery_date": payment_deadline,
				"set_warehouse": settings.plot_inventory_warehouse,
				"ignore_default_payment_terms_template": 1,
				# Custom fields (fixtures in Phase 1):
				"plot": plot.name,
				"land_acquisition": plot.land_acquisition,
				"acquisition_name": plot.acquisition_name,
				"plot_application": self.name,
				"booking_fee_percent": flt(plot.booking_fee_percent),
				"government_share_percent": flt(plot.government_share_percent),
				"payment_completion_days": payment_completion_days,
				"payment_deadline": payment_deadline,
				"payment_schedule": payment_schedule_rows,
				"items": [item_row],
			}
		)
		so.insert(ignore_permissions=True)

		self.db_set("sales_order", so.name)

		if notify:
			frappe.msgprint(
				f"Sales Order <b>{so.name}</b> created. You can now review and submit it.",
				indicator="green",
				alert=True,
			)
		return so.name


def _why_cancelled(application, reason):
	"""A few words for the history line when an application is cancelled with something else."""
	if reason == "Contract Terminated":
		return "contract terminated"
	if application.flags.get("from_sales_order_cancel"):
		return "order cancelled"
	return ""
