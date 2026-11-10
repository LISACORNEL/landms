from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.query_builder import Order
from frappe.utils import add_days, cint, escape_html, flt, get_url_to_form, getdate, today

from landms.landms.doctype.land_acquisition.land_acquisition import (
	sync_land_acquisition_plot_summary,
)
from landms.landms.doctype.plot_master.plot_status import link, set_plot_status
from landms.sales_order_hooks import _post_credit_note_for_outstanding


class PlotContract(Document):
	# ------------------------------------------------------------------ #
	#  Validate / Submit / Cancel                                          #
	# ------------------------------------------------------------------ #

	def validate(self):
		self.validate_plot_available()
		self.fill_selling_price()
		self.calculate_financials()
		self.generate_payment_schedule()
		self.calculate_payment_summary()

	def before_submit(self):
		self._validate_sales_order_first_payment_gate()
		self._validate_advance_met()

	def before_cancel(self):
		if flt(self.total_paid) > 0:
			frappe.throw(
				f"Contract {self.name} has received payments (TZS {flt(self.total_paid):,.0f}). "
				"Use Terminate Contract instead of Cancel."
			)

	def on_submit(self):
		set_plot_status(self.plot, "Reserved", f"contract {link('Plot Contract', self.name)} was submitted")
		self._sync_land_acquisition_summary()
		self.db_set("contract_status", "Ongoing")

	def on_cancel(self):
		set_plot_status(self.plot, "Available", f"contract {link('Plot Contract', self.name)} was cancelled")
		self._sync_land_acquisition_summary()
		self.db_set("contract_status", "Cancelled")

	# ------------------------------------------------------------------ #
	#  Validation helpers                                                  #
	# ------------------------------------------------------------------ #

	def validate_plot_available(self):
		if not self.plot:
			return
		if self.sales_order or self.flags.get("from_sales_order"):
			return
		if frappe.db.exists("Plot Contract", self.name):
			return

		plot_status = frappe.db.get_value("Plot Master", self.plot, "status")
		if plot_status != "Available":
			frappe.throw(
				f"Plot {self.plot} is not Available (current status: {plot_status}). "
				"Only Available plots can be contracted."
			)
		active = frappe.db.exists(
			"Plot Contract",
			{
				"plot": self.plot,
				"docstatus": 1,
				"contract_status": ["in", ["Ongoing", "Completed"]],
			},
		)
		if active:
			frappe.throw(
				f"Plot {self.plot} already has an active contract ({active}). Terminate or complete it first."
			)

	def _validate_sales_order_first_payment_gate(self):
		if not self.sales_order:
			return

		if not frappe.db.exists("Sales Order", self.sales_order):
			frappe.throw(f"Linked Sales Order {self.sales_order} was not found.")

		so_doc = frappe.get_doc("Sales Order", self.sales_order)
		if so_doc.docstatus != 1:
			frappe.throw(f"Linked Sales Order {so_doc.name} is not submitted. Submit the Sales Order first.")
		if so_doc.plot != self.plot or so_doc.customer != self.customer:
			frappe.throw(
				f"Contract {self.name} does not match linked Sales Order {so_doc.name} "
				"(plot/customer mismatch)."
			)
		if not self._sales_order_has_any_confirmed_payment(so_doc):
			frappe.throw(
				f"Cannot submit contract {self.name} before first payment on Sales Order "
				f"{so_doc.name}. Record payment on the Sales Order first."
			)

	def _sales_order_has_any_confirmed_payment(self, so_doc):
		plot_invoice = so_doc.get("plot_sales_invoice")
		if not plot_invoice or not frappe.db.exists("Sales Invoice", plot_invoice):
			return bool(flt(so_doc.get("advance_paid")))

		si = frappe.db.get_value(
			"Sales Invoice",
			plot_invoice,
			["docstatus", "outstanding_amount", "grand_total"],
			as_dict=True,
		)
		return bool(si and si.docstatus == 1 and flt(si.outstanding_amount) < flt(si.grand_total))

	def _validate_advance_met(self):
		"""Block submission unless the advance installment is fully paid."""
		invoice_name = self._get_plot_invoice_name()
		if not invoice_name:
			return
		si_doc = frappe.get_doc("Sales Invoice", invoice_name)
		if si_doc.docstatus != 1:
			return
		if self._is_advance_installment_met(si_doc):
			return

		grand_total = flt(si_doc.grand_total)
		advance_expected = (
			flt(grand_total * flt(self.booking_fee_percent) / 100)
			if grand_total and self.booking_fee_percent
			else 0.0
		)
		total_paid = max(0.0, grand_total - flt(si_doc.outstanding_amount))
		frappe.throw(
			f"Cannot submit contract — advance of TZS {advance_expected:,.0f} has not been met. "
			f"Only TZS {total_paid:,.0f} received so far."
		)

	# ------------------------------------------------------------------ #
	#  Field calculation helpers                                           #
	# ------------------------------------------------------------------ #

	def fill_selling_price(self):
		if not self.plot:
			return

		plot_data = frappe.db.get_value(
			"Plot Master",
			self.plot,
			["selling_price", "land_acquisition"],
			as_dict=True,
		)
		if not plot_data:
			return

		if not flt(self.selling_price):
			self.selling_price = plot_data.selling_price
		self.land_acquisition = plot_data.land_acquisition
		if plot_data.land_acquisition:
			self.acquisition_name = (
				frappe.db.get_value(
					"Land Acquisition",
					plot_data.land_acquisition,
					"acquisition_name",
				)
				or ""
			)

		so_doc = self._get_sales_order_doc()
		if so_doc:
			self.control_number = so_doc.get("control_number") or self.control_number
			self.plot_application = so_doc.get("plot_application") or self.plot_application
			self.booking_fee_invoice = so_doc.get("plot_sales_invoice") or self.booking_fee_invoice

	def calculate_financials(self):
		if flt(self.selling_price) > 0 and flt(self.booking_fee_percent) > 0:
			self.booking_fee_amount = flt(self.selling_price) * flt(self.booking_fee_percent) / 100
			self.balance_due = flt(self.selling_price) - flt(self.booking_fee_amount)
		if self.contract_date and flt(self.payment_completion_days) > 0:
			self.payment_deadline = add_days(self.contract_date, int(self.payment_completion_days))

	def generate_payment_schedule(self):
		"""Only standalone manual contracts generate their own schedule.

		Sales-Order-linked contracts are read-only mirrors of the Sales Order /
		Sales Invoice schedule and are populated by sales_order_hooks + syncs.
		"""
		if self.docstatus == 1:
			return
		if self.sales_order or self.flags.get("from_sales_order"):
			return
		if not flt(self.selling_price) or not flt(self.booking_fee_percent):
			return
		if not self.contract_date:
			return

		booking_fee = flt(self.booking_fee_amount) or (
			flt(self.selling_price) * flt(self.booking_fee_percent) / 100
		)
		balance = flt(self.selling_price) - booking_fee
		total_days = int(self.payment_completion_days or 90)

		self.set("payment_schedule", [])
		self.append(
			"payment_schedule",
			{
				"installment_number": 1,
				"description": "Advance",
				"due_date": self.contract_date,
				"expected_amount": booking_fee,
				"paid_amount": 0,
				"outstanding_amount": booking_fee,
				"paid_date": None,
				"sales_invoice": "",
				"status": "Pending",
			},
		)

		if balance > 0:
			self.append(
				"payment_schedule",
				{
					"installment_number": 2,
					"description": "Balance",
					"due_date": add_days(self.contract_date, total_days),
					"expected_amount": balance,
					"paid_amount": 0,
					"outstanding_amount": balance,
					"paid_date": None,
					"sales_invoice": "",
					"status": "Pending",
				},
			)

	def calculate_payment_summary(self):
		# For SO-linked contracts, payment totals are managed by
		# sync_payment_status (which reads from the SI). Recalculating
		# from in-memory schedule rows here would overwrite synced values
		# with stale data because the schedule rows are DB-updated, not
		# in-memory-updated, during sync.
		if self.sales_order and flt(self.total_paid) > 0:
			self.total_contract_value = flt(self.selling_price)
			if flt(self.government_share_percent) > 0:
				self.government_fee_withheld = (
					flt(self.selling_price) * flt(self.government_share_percent) / 100
				)
			return

		self.total_contract_value = flt(self.selling_price)
		total_paid = sum(flt(row.paid_amount) for row in self.payment_schedule)
		self.total_paid = total_paid
		self.total_outstanding = max(0.0, flt(self.selling_price) - total_paid)
		self.payment_progress = self._derive_payment_progress(total_paid, self.total_outstanding)
		if flt(self.government_share_percent) > 0:
			self.government_fee_withheld = flt(self.selling_price) * flt(self.government_share_percent) / 100

	def _derive_payment_progress(self, total_paid, total_outstanding, si_doc=None):
		if flt(total_paid) <= 0:
			return "Unpaid"
		if flt(total_outstanding) <= 0:
			return "Fully Paid"

		# Determine advance threshold from the first schedule row's portion.
		# Use total_paid (grand_total − outstanding) for comparison — NOT the
		# per-row paid_amount which ERPNext may spread unpredictably.
		advance_expected = 0.0
		if si_doc:
			grand_total = flt(si_doc.grand_total)
			advance_expected = (
				flt(grand_total * flt(self.booking_fee_percent) / 100)
				if grand_total and self.booking_fee_percent
				else 0.0
			)
		else:
			for row in self.payment_schedule:
				if cint(row.installment_number or 0) == 1:
					advance_expected = flt(row.expected_amount)
					break

		if advance_expected > 0 and flt(total_paid) >= advance_expected:
			balance_paid = flt(total_paid) - advance_expected
			if balance_paid > 0:
				return "Advance + Installments Paid"
			return "Advance Paid"

		return "Partially Paid"

	def _is_advance_installment_met(self, si_doc):
		"""Check whether enough money has been received to cover the advance.

		Uses total money received (grand_total − outstanding) rather than the
		per-row paid_amount, because ERPNext may spread allocations across
		schedule rows unpredictably.
		"""
		grand_total = flt(si_doc.grand_total)
		total_paid = max(0.0, grand_total - flt(si_doc.outstanding_amount))
		if total_paid <= 0:
			return False

		advance_expected = (
			flt(grand_total * flt(self.booking_fee_percent) / 100)
			if grand_total and self.booking_fee_percent
			else 0.0
		)
		if advance_expected > 0:
			return total_paid >= advance_expected

		# No schedule rows — any payment counts.
		return True

	def _sync_land_acquisition_summary(self):
		la = self.land_acquisition or frappe.db.get_value(
			"Plot Master",
			self.plot,
			"land_acquisition",
		)
		if la:
			sync_land_acquisition_plot_summary(la)

	# ------------------------------------------------------------------ #
	#  Sales Order / Invoice link helpers                                  #
	# ------------------------------------------------------------------ #

	def _get_sales_order_doc(self):
		if not self.sales_order or not frappe.db.exists("Sales Order", self.sales_order):
			return None
		return frappe.get_doc("Sales Order", self.sales_order)

	def _get_plot_invoice_name(self):
		so_doc = self._get_sales_order_doc()
		if so_doc:
			invoice_name = so_doc.get("plot_sales_invoice") or ""
			if invoice_name and frappe.db.exists("Sales Invoice", invoice_name):
				return invoice_name
		if self.booking_fee_invoice and frappe.db.exists("Sales Invoice", self.booking_fee_invoice):
			return self.booking_fee_invoice
		return ""

	# ------------------------------------------------------------------ #
	#  Payment sync (single-SI mirror)                                     #
	# ------------------------------------------------------------------ #

	def sync_payment_status(self):
		so_doc = self._get_sales_order_doc()
		if not so_doc:
			return

		invoice_name = self._get_plot_invoice_name()
		if not invoice_name:
			self._sync_header_from_sales_order(so_doc)
			self._persist_payment_sync_state()
			return

		si_doc = frappe.get_doc("Sales Invoice", invoice_name)
		if si_doc.docstatus != 1:
			return

		total_paid = max(0.0, flt(si_doc.grand_total) - flt(si_doc.outstanding_amount))
		total_outstanding = max(0.0, flt(si_doc.outstanding_amount))
		overpaid_amount = max(0.0, -(flt(si_doc.outstanding_amount)))
		paid_dates = self._get_paid_dates_by_installment(si_doc)

		if len(self.payment_schedule or []) != len(si_doc.payment_schedule or []) and self.docstatus == 0:
			self._rebuild_schedule_from_invoice(si_doc)

		self._sync_header_from_sales_order(so_doc, invoice_name=si_doc.name)
		self._sync_schedule_rows_from_invoice(si_doc, paid_dates)
		self.total_contract_value = flt(si_doc.grand_total)
		self.total_paid = total_paid
		self.total_outstanding = total_outstanding
		self.overpaid_amount = overpaid_amount
		self.payment_progress = self._derive_payment_progress(total_paid, total_outstanding, si_doc=si_doc)
		self._persist_payment_sync_state()

		advance_met = self._is_advance_installment_met(si_doc)

		# Only auto-submit once the synced draft state is already safely saved.
		# Reload before submitting to avoid TimestampMismatchError from concurrent syncs.
		if advance_met and self.docstatus == 0:
			try:
				self.reload()
				if self.docstatus == 0:
					self.submit()
					self.reload()
			except frappe.TimestampMismatchError:
				pass

		if so_doc.get("plot_application"):
			app_status = frappe.db.get_value("Plot Application", so_doc.plot_application, "status")
			if advance_met and app_status == "Paid":
				frappe.db.set_value(
					"Plot Application",
					so_doc.plot_application,
					"status",
					"Converted",
					update_modified=True,
				)
				frappe.clear_document_cache("Plot Application", so_doc.plot_application)

		if total_outstanding <= 0 and self.docstatus == 1:
			self.contract_status = "Completed"
			set_plot_status(
				self.plot,
				"Ready for Handover",
				f"contract {link('Plot Contract', self.name)} is fully paid",
				# every status except handed over ("" = a plot with no status yet, as before)
				only_from=(
					"",
					"Available",
					"Pending Fee",
					"Pending Advance",
					"Reserved",
					"Ready for Handover",
				),
			)
			self._sync_land_acquisition_summary()

			# Persist the submitted contract state before posting linked entries.
			self._persist_payment_sync_state()
			settings = frappe.get_single("LandMS Settings")
			self.reload()
			je_name = self._post_completion_entries(settings)
			handover_name = self._ensure_handover_draft()

			msg = f"Contract fully paid. Plot {self.plot} marked as Ready for Handover."
			if je_name:
				msg += f" Government fee posted — Journal Entry: {je_name}."
			if handover_name:
				msg += f" Plot Handover <b>{handover_name}</b> created (Draft) — fill the handover parties and submit."
			frappe.msgprint(msg, indicator="green", alert=True)
			return  # already saved above

		elif advance_met and self.docstatus == 1:
			self.contract_status = "Ongoing"
			if set_plot_status(
				self.plot,
				"Reserved",
				f"the advance on contract {link('Plot Contract', self.name)} is paid",
				only_from=("Pending Advance",),
			):
				self._sync_land_acquisition_summary()

		self._persist_payment_sync_state()

	def _sync_header_from_sales_order(self, so_doc, *, invoice_name: str | None = None):
		self.plot_application = so_doc.get("plot_application") or ""
		self.control_number = so_doc.get("control_number") or ""
		self.booking_fee_invoice = invoice_name or so_doc.get("plot_sales_invoice") or ""
		self.payment_deadline = so_doc.get("payment_deadline")

	def _rebuild_schedule_from_invoice(self, invoice):
		grand_total = flt(invoice.grand_total)
		booking_fee_percent = flt(self.booking_fee_percent or 0)
		total_paid = max(0.0, grand_total - flt(invoice.outstanding_amount))
		advance_amount = (
			flt(grand_total * booking_fee_percent / 100)
			if grand_total and booking_fee_percent
			else grand_total
		)
		balance_amount = flt(grand_total - advance_amount)
		row1_paid = min(total_paid, advance_amount)
		row2_paid = max(0.0, total_paid - row1_paid)

		self.set("payment_schedule", [])
		for idx, row in enumerate(invoice.get("payment_schedule") or [], start=1):
			expected = advance_amount if idx == 1 else balance_amount
			paid_amount = row1_paid if idx == 1 else row2_paid
			outstanding = max(0.0, expected - paid_amount)
			self.append(
				"payment_schedule",
				{
					"installment_number": idx,
					"description": row.description or self._default_installment_label(idx),
					"due_date": row.due_date,
					"expected_amount": expected,
					"paid_amount": paid_amount,
					"outstanding_amount": outstanding,
					"paid_date": None,
					"sales_invoice": invoice.name,
					"status": self._derive_installment_status(row.due_date, expected, outstanding),
				},
			)
		self.save(ignore_permissions=True)

	def _sync_schedule_rows_from_invoice(self, invoice, paid_dates):
		today_date = getdate(today())
		grand_total = flt(invoice.grand_total)
		booking_fee_percent = flt(self.booking_fee_percent or 0)
		total_paid = max(0.0, grand_total - flt(invoice.outstanding_amount))
		advance_amount = (
			flt(grand_total * booking_fee_percent / 100)
			if grand_total and booking_fee_percent
			else grand_total
		)
		balance_amount = flt(grand_total - advance_amount)
		row1_paid = min(total_paid, advance_amount)
		row2_paid = max(0.0, total_paid - row1_paid)

		rows = sorted(self.payment_schedule, key=lambda d: cint(d.installment_number or 0))

		for idx, source in enumerate(invoice.get("payment_schedule") or [], start=1):
			if idx > len(rows):
				break

			target = rows[idx - 1]
			existing_expected = flt(target.expected_amount)
			if existing_expected > 0:
				expected = existing_expected
			else:
				expected = advance_amount if idx == 1 else balance_amount
			paid_amount = row1_paid if idx == 1 else row2_paid
			outstanding = max(0.0, expected - paid_amount)
			status = self._derive_installment_status(
				source.due_date, expected, outstanding, today_date=today_date
			)
			paid_date = paid_dates.get(idx) if outstanding <= 0 else None

			target.description = source.description or self._default_installment_label(idx)
			target.due_date = source.due_date
			target.expected_amount = expected
			target.paid_amount = paid_amount
			target.outstanding_amount = outstanding
			target.paid_date = paid_date
			target.sales_invoice = invoice.name
			target.status = status
			if self.docstatus == 1 and target.name:
				target.db_update()

	def _persist_payment_sync_state(self):
		if self.docstatus == 0:
			frappe.db.set_value(
				"Plot Contract",
				self.name,
				{
					"total_contract_value": flt(self.total_contract_value),
					"total_paid": flt(self.total_paid),
					"total_outstanding": flt(self.total_outstanding),
					"overpaid_amount": flt(self.overpaid_amount),
					"payment_progress": self.payment_progress or "",
					"contract_status": self.contract_status or "Draft",
					"booking_fee_invoice": self.booking_fee_invoice or "",
					"payment_deadline": self.payment_deadline,
				},
				update_modified=True,
			)
			# Also persist individual payment schedule rows for draft contracts
			for row in self.payment_schedule or []:
				if row.name:
					row.db_update()
			frappe.clear_document_cache("Plot Contract", self.name)
			return

		frappe.db.set_value(
			"Plot Contract",
			self.name,
			{
				"plot_application": self.plot_application or "",
				"control_number": self.control_number or "",
				"booking_fee_invoice": self.booking_fee_invoice or "",
				"payment_deadline": self.payment_deadline,
				"total_contract_value": flt(self.total_contract_value),
				"total_paid": flt(self.total_paid),
				"total_outstanding": flt(self.total_outstanding),
				"overpaid_amount": flt(self.overpaid_amount),
				"payment_progress": self.payment_progress or "",
				"contract_status": self.contract_status or "Draft",
			},
			update_modified=True,
		)
		frappe.clear_document_cache("Plot Contract", self.name)

	def _derive_installment_status(self, due_date, expected, outstanding, *, today_date=None):
		today_date = today_date or getdate(today())
		if expected > 0 and outstanding <= 0:
			return "Paid"
		if due_date and getdate(str(due_date)) < today_date:
			return "Overdue"
		return "Pending"

	def _default_installment_label(self, idx):
		return "Advance" if idx == 1 else f"Installment {idx}"

	def _get_paid_dates_by_installment(self, invoice):
		grand_total = flt(invoice.grand_total)
		booking_fee_percent = flt(self.booking_fee_percent or 0)
		advance_amount = (
			flt(grand_total * booking_fee_percent / 100)
			if grand_total and booking_fee_percent
			else grand_total
		)
		balance_amount = flt(grand_total - advance_amount)
		schedule = invoice.get("payment_schedule") or []
		thresholds = [advance_amount + (balance_amount * (i > 0)) for i in range(len(schedule))]

		payment_events = []
		for advance in invoice.get("advances") or []:
			posting_date = self._get_payment_source_date(advance.reference_type, advance.reference_name)
			if posting_date and flt(advance.allocated_amount):
				payment_events.append(
					{
						"posting_date": posting_date,
						"amount": flt(advance.allocated_amount),
						"name": advance.reference_name,
					}
				)

		payment_events.extend(self._get_invoice_payment_entry_events(invoice.name))
		payment_events.sort(key=lambda d: (d["posting_date"], d["name"]))

		paid_dates = {}
		cumulative_paid = 0.0
		threshold_idx = 0
		for event in payment_events:
			cumulative_paid += flt(event["amount"])
			while threshold_idx < len(thresholds) and cumulative_paid >= thresholds[threshold_idx]:
				paid_dates[threshold_idx + 1] = event["posting_date"]
				threshold_idx += 1

		return paid_dates

	def _get_payment_source_date(self, reference_type, reference_name):
		if not reference_type or not reference_name or not frappe.db.exists(reference_type, reference_name):
			return None
		if reference_type == "Payment Entry":
			return frappe.db.get_value("Payment Entry", reference_name, "posting_date")
		if reference_type == "Journal Entry":
			return frappe.db.get_value("Journal Entry", reference_name, "posting_date")
		return None

	def _get_invoice_payment_entry_events(self, invoice_name):
		pe = frappe.qb.DocType("Payment Entry")
		per = frappe.qb.DocType("Payment Entry Reference")
		return (
			frappe.qb.from_(pe)
			.inner_join(per)
			.on(per.parent == pe.name)
			.select(pe.posting_date, per.allocated_amount.as_("amount"), pe.name)
			.where(pe.docstatus == 1)
			.where(per.reference_doctype == "Sales Invoice")
			.where(per.reference_name == invoice_name)
			.orderby(pe.posting_date, order=Order.asc)
			.orderby(pe.name, order=Order.asc)
			.run(as_dict=True)
		)

	# ------------------------------------------------------------------ #
	#  GL Entry helpers                                                    #
	# ------------------------------------------------------------------ #

	def _post_completion_entries(self, settings):
		if self.government_fee_entry:
			return None

		selling_price = flt(self.selling_price)
		govt_fee = flt(self.government_fee_withheld)
		net_revenue = selling_price - govt_fee

		if selling_price <= 0:
			return None

		accounts = [
			{
				"account": settings.customer_advance_account,
				"debit_in_account_currency": net_revenue,
				"cost_center": settings.cost_center,
				"land_acquisition": self.land_acquisition,
			},
			{
				"account": settings.revenue_account,
				"credit_in_account_currency": net_revenue,
				"cost_center": settings.cost_center,
				"land_acquisition": self.land_acquisition,
			},
		]

		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"posting_date": today(),
				"company": settings.company,
				"voucher_type": "Journal Entry",
				"user_remark": (
					f"Revenue recognition — Contract {self.name}, Plot {self.plot}, Customer {self.customer}"
				),
				"accounts": accounts,
			}
		)
		je.insert(ignore_permissions=True)
		je.submit()
		self.db_set("government_fee_entry", je.name)
		return je.name

	def _ensure_handover_draft(self):
		existing = frappe.db.get_value(
			"Plot Handover",
			{"contract": self.name, "docstatus": ["!=", 2]},
			"name",
		)
		if existing:
			return existing

		handover = frappe.get_doc(
			{
				"doctype": "Plot Handover",
				"contract": self.name,
				"handover_date": today(),
				"customer": self.customer,
				"plot": self.plot,
				"acquisition_name": self.get("acquisition_name") or "",
				"land_acquisition": self.get("land_acquisition") or "",
				"contract_date": self.get("contract_date"),
				"selling_price": flt(self.selling_price),
			}
		)
		handover.flags.ignore_permissions = True
		handover.flags.ignore_mandatory = True
		handover.insert()
		return handover.name

	def _post_termination_journal_entry(self, settings):
		if self.forfeiture_entry:
			return None

		total_paid = flt(self.total_paid)
		if total_paid <= 0:
			return None

		forfeiture_pct = flt(settings.forfeiture_percentage or 100)
		govt_pct = flt(self.government_share_percent or 0)
		forfeited_amount = total_paid * forfeiture_pct / 100.0
		# The government share was already moved to Government Payable as each payment
		# came in (see payment_sync._post_government_share_je), so only the company's
		# NET take — forfeited amount minus that already-withheld govt share — is new
		# income here. The customer's refund stays parked in Customer Advance for the
		# accountant to pay out manually (tracked via lms_refund_amount).
		govt_withheld = total_paid * govt_pct / 100.0
		forfeiture_income = forfeited_amount - govt_withheld
		refund_amount = total_paid - forfeited_amount
		if forfeiture_income <= 0:
			return None

		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"posting_date": today(),
				"company": settings.company,
				"voucher_type": "Journal Entry",
				"lms_refund_amount": refund_amount,
				"user_remark": (
					f"Contract termination — {forfeiture_pct:.0f}% of paid amount forfeited, "
					f"net of {govt_pct:.2f}% government share already withheld. "
					f"Contract {self.name}, Plot {self.plot}, Customer {self.customer}"
				),
				"accounts": [
					{
						"account": settings.customer_advance_account,
						"debit_in_account_currency": forfeiture_income,
						"cost_center": settings.cost_center,
						"land_acquisition": self.land_acquisition,
					},
					{
						"account": settings.forfeited_deposits_account,
						"credit_in_account_currency": forfeiture_income,
						"cost_center": settings.cost_center,
						"land_acquisition": self.land_acquisition,
					},
				],
			}
		)
		je.insert(ignore_permissions=True)
		je.submit()
		self.db_set("forfeiture_entry", je.name)
		return je.name

	# ------------------------------------------------------------------ #
	#  Contract termination                                                #
	# ------------------------------------------------------------------ #

	@frappe.whitelist()
	def terminate_contract(self, reason):
		if self.contract_status not in ("Ongoing", "Overdue"):
			frappe.throw("Only Ongoing or Overdue contracts can be terminated.")
		if self.docstatus != 1:
			frappe.throw("Document must be submitted before it can be terminated.")
		if not reason or not str(reason).strip():
			frappe.throw("A termination reason is required.")

		settings = frappe.get_single("LandMS Settings")
		self.reload()

		# DECLINE FIRST — gate the entire termination on the TCB decline. If the
		# control number cannot be declined (e.g. TCB unreachable), throw and change
		# NOTHING (no forfeiture JE, no SI/SO changes, no plot release). Mirrors the
		# decline-first gate in close_sales_order / cancel_sales_order.
		self._decline_control_number_on_termination_or_block()

		# Mark terminated first so subsequent hooks see the correct contract state
		self.db_set("contract_status", "Terminated")
		self.db_set("termination_reason", str(reason).strip())

		# Post forfeiture JE while Customer Advance liability balance still exists
		# (must happen before any SI/SO cancellations reverse the GL entries)
		je_name = self._post_termination_journal_entry(settings)

		# Cancel the SI only if it has no payment allocations — if payments exist
		# the forfeiture JE above already handles the accounting
		self._cancel_plot_invoice_on_termination()

		# If the SI was left open (payments existed), issue a credit note to clear AR
		si_name = self._get_plot_invoice_name()
		if si_name and flt(frappe.db.get_value("Sales Invoice", si_name, "outstanding_amount") or 0) > 0:
			_post_credit_note_for_outstanding(si_name)

		# Close the linked Sales Order (status=Closed, docstatus preserved). The TCB
		# control number was already declined by the decline-first gate above.
		self._cancel_linked_sales_order_on_termination()

		# Cancel the Plot Application so the plot is fully released
		self._cancel_plot_application_on_termination()

		set_plot_status(
			self.plot,
			"Available",
			f"contract {link('Plot Contract', self.name)} was terminated: {escape_html(str(reason))}",
		)
		self._sync_land_acquisition_summary()

		msg = f"Contract terminated. Plot {self.plot} is now Available for new contracts."
		if je_name:
			total_paid = flt(self.total_paid)
			forfeiture_pct = flt(settings.forfeiture_percentage or 100)
			forfeited_amount = total_paid * forfeiture_pct / 100.0
			msg += (
				f" TZS {forfeited_amount:,.0f} ({forfeiture_pct:.0f}% of TZS {total_paid:,.0f} paid) forfeited — "
				f"Journal Entry: {je_name}."
			)
		frappe.msgprint(msg, indicator="orange", alert=True)

	def _cancel_plot_invoice_on_termination(self):
		"""Cancel the plot SI during termination only if no payment allocations exist.

		If payments have been applied (outstanding < grand_total) the SI is left
		submitted as an audit trail. The forfeiture JE moves the Customer Advance
		liability to Forfeited Deposits income — cancelling a paid SI would reverse
		that GL and create a negative AR balance.
		"""
		invoice_name = self._get_plot_invoice_name()
		if not invoice_name:
			return
		si_doc = frappe.get_doc("Sales Invoice", invoice_name)
		if si_doc.docstatus == 0:
			frappe.delete_doc("Sales Invoice", invoice_name, force=True, ignore_permissions=True)
			return
		if si_doc.docstatus != 1:
			return
		# Only cancel when no payments have been allocated (fully outstanding)
		if flt(si_doc.outstanding_amount) >= flt(si_doc.grand_total):
			si_doc.flags.ignore_links = True
			si_doc.cancel()
		# If any payment exists, leave the SI — forfeiture JE handles the accounting

	def _decline_control_number_on_termination_or_block(self):
		"""DECLINE FIRST — decline the linked Sales Order's TCB control number as the
		gate for termination. If it cannot be declined (e.g. TCB unreachable), abort
		the whole termination BEFORE any forfeiture JE, plot release, or SO close, so
		nothing is left half-done and no live orphaned control number remains. An
		already-declined number returns ok=True, so a retry passes straight through.
		"""
		from frappe.utils import cstr

		from landms.sales_order_hooks import decline_reference_for_sales_order

		so_name = self.sales_order
		if not so_name or not frappe.db.exists("Sales Order", so_name):
			return
		control_number = cstr(frappe.db.get_value("Sales Order", so_name, "control_number") or "").strip()
		if not control_number:
			return
		# final: a terminated contract never reopens, so rails that follow the
		# decline (Malipopay) end their copy of the number instead of pausing it
		result = decline_reference_for_sales_order(so_name, control_number, final=True)
		if not result.get("ok"):
			frappe.throw(
				f"This contract could not be terminated yet because the bank could "
				f"not be reached to release the payment number. Nothing has been "
				f"changed on this contract. Please try terminating it again after a "
				f"while. (Ref {control_number}: {result.get('message')})"
			)

	def _cancel_linked_sales_order_on_termination(self):
		"""Close the Sales Order on termination — keeps docstatus=1 as audit trail.

		Sets status='Closed' directly (same as the Close button) rather than
		cancelling, so accounting entries are preserved. The TCB control number is
		declined UP FRONT by the decline-first gate in terminate_contract, so this
		method only flips the SO to Closed.
		"""
		so_name = self.sales_order
		if not so_name or not frappe.db.exists("Sales Order", so_name):
			return
		so_doc = frappe.get_doc("Sales Order", so_name)
		if so_doc.docstatus != 1:
			return

		# Close instead of cancel — preserves docstatus=1 and all accounting.
		# update_modified=True + a timeline comment leave an audit footprint of who/what
		# closed the Sales Order (mirrors the Close button), since this close is
		# triggered by the contract termination.
		frappe.db.set_value("Sales Order", so_name, "status", "Closed", update_modified=True)
		so_doc.add_comment(
			"Info",
			f"Sales Order closed via termination of contract {self.name} by {frappe.session.user}.",
		)

	def _cancel_plot_application_on_termination(self):
		"""Cancel the Plot Application so the plot is fully released."""
		app_name = self.plot_application
		if not app_name:
			so_doc = self._get_sales_order_doc()
			if so_doc:
				app_name = so_doc.get("plot_application")
		if not app_name or not frappe.db.exists("Plot Application", app_name):
			return
		app_doc = frappe.get_doc("Plot Application", app_name)
		if app_doc.docstatus != 1:
			return
		app_doc.flags.ignore_permissions = True
		app_doc.flags.ignore_links = True
		app_doc.flags._cancellation_reason = "Contract Terminated"
		app_doc.cancel()


@frappe.whitelist()
def get_linked_documents_summary(plot_contract):
	if not plot_contract or not frappe.db.exists("Plot Contract", plot_contract):
		return ""

	doc = frappe.get_doc("Plot Contract", plot_contract)
	so_doc = doc._get_sales_order_doc()
	plot_application = doc.plot_application or (so_doc.get("plot_application") if so_doc else "")
	application_doc = (
		frappe.get_doc("Plot Application", plot_application)
		if plot_application and frappe.db.exists("Plot Application", plot_application)
		else None
	)

	rows = [
		("Sales Order", _doc_link("Sales Order", doc.sales_order)),
		("TCB Control Number", escape_html(doc.control_number or "-")),
		("Plot Sales Invoice", _doc_link("Sales Invoice", doc.booking_fee_invoice)),
	]

	if application_doc:
		rows.extend(
			[
				("Plot Application", _doc_link("Plot Application", application_doc.name)),
				("Application Fee Invoice", _doc_link("Sales Invoice", application_doc.sales_invoice)),
				("Application Fee Payment Entry", _doc_link("Payment Entry", application_doc.payment_entry)),
			]
		)

	if doc.government_fee_entry:
		rows.append(("Government Fee JE", _doc_link("Journal Entry", doc.government_fee_entry)))
	if doc.forfeiture_entry:
		rows.append(("Forfeiture JE", _doc_link("Journal Entry", doc.forfeiture_entry)))

	payment_entries = _get_plot_payment_entries(doc)
	payment_entries_html = "<span class='text-muted'>-</span>"
	if payment_entries:
		payment_entries_html = "<br>".join(
			_doc_link("Payment Entry", row.name)
			+ f" <span class='text-muted'>({escape_html(str(row.posting_date))})</span>"
			for row in payment_entries
		)

	body = "".join(
		f"""
		<tr>
			<td style="padding:10px; font-weight:600; white-space:nowrap;">{escape_html(label)}</td>
			<td style="padding:10px;">{value or "<span class='text-muted'>-</span>"}</td>
		</tr>
		"""
		for label, value in rows
	)
	body += f"""
	<tr>
		<td style="padding:10px; font-weight:600; white-space:nowrap;">Payment Entries</td>
		<td style="padding:10px;">{payment_entries_html}</td>
	</tr>
	"""

	return f"""
	<div class="table-responsive">
		<table class="table table-bordered" style="margin-bottom:0; font-size:13px;">
			<tbody>{body}</tbody>
		</table>
	</div>
	"""


def _doc_link(doctype, name):
	if not name or not frappe.db.exists(doctype, name):
		return "<span class='text-muted'>-</span>"
	url = get_url_to_form(doctype, name)
	return f"<a href='{escape_html(url)}' target='_blank'>{escape_html(name)}</a>"


def _get_plot_payment_entries(doc):
	names = set()
	rows = []

	for reference_doctype, reference_name in (
		("Sales Order", doc.sales_order),
		("Sales Invoice", doc.booking_fee_invoice),
	):
		if not reference_name:
			continue
		for row in _get_submitted_payment_entries(reference_doctype, reference_name):
			if row.name not in names:
				names.add(row.name)
				rows.append(row)

	rows.sort(key=lambda d: (d.posting_date, d.name))
	return rows


def _get_submitted_payment_entries(reference_doctype, reference_name):
	pe = frappe.qb.DocType("Payment Entry")
	per = frappe.qb.DocType("Payment Entry Reference")
	return (
		frappe.qb.from_(pe)
		.inner_join(per)
		.on(per.parent == pe.name)
		.select(pe.name, pe.posting_date)
		.distinct()
		.where(pe.docstatus == 1)
		.where(per.reference_doctype == reference_doctype)
		.where(per.reference_name == reference_name)
		.orderby(pe.posting_date, order=Order.asc)
		.orderby(pe.name, order=Order.asc)
		.run(as_dict=True)
	)
