"""The project form (Land Acquisition): its checks, approval and cancel, and the plot and period
summaries it shows (LA-2).

Its other jobs live next to it: cost_totals.py (what the land has cost), recalculate_plot_costs.py
(the Recalculate Plot Costs button) and landms/overrides/update_project_costs.py (rules that run when a
purchase, journal or payment is saved). The form's two moved buttons, and the
recalculation job, still answer at their old address here, for pages opened and jobs queued before
an update.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, today

from landms.after_submit import drop_rounding_noise_on_locked_fields
from landms.landms.doctype.land_acquisition.cost_totals import (
	get_land_acquisition_cost_summary,  # noqa: F401 - old address kept
	refresh_cost_summary,  # noqa: F401 - old address kept
	sync_land_acquisition_cost_summary,
)
from landms.landms.doctype.land_acquisition.recalculate_plot_costs import (  # noqa: F401 - old addresses kept
	_run_plot_cost_recalculation,  # a job queued before an update still finds it
	recalculate_plot_costs,
)


class LandAcquisition(Document):
	def validate(self):
		self._convert_payment_period_to_days()
		self._validate_area()
		self._validate_coordinates()
		self._validate_sales_defaults()
		self._validate_plot_type_rates()
		self._validate_channel_roles()
		self._validate_and_set_tcb_patterns()

	def _validate_channel_roles(self):
		"""One issuer, ticked; every other ticked channel is its dependent.

		The issuer (payment_channel, 'Control Numbers Issued By') is the
		independent channel whose numbering mints this project's references.
		It must be one of the enabled channels — a project cannot issue
		numbers through a channel it does not collect with.
		"""
		enabled = {
			"TCB": cint(self.get("collect_via_tcb")),
			"Malipopay": cint(self.get("collect_via_malipopay")),
		}
		if not any(enabled.values()):
			frappe.throw(_("Enable at least one payment channel (tick TCB or Malipopay)."))

		enabled_names = [name for name, on in enabled.items() if on]
		issuer = (self.get("payment_channel") or "").strip()

		# Exactly one channel ticked -> it IS the issuer; set it so the operator
		# never has to state (or correct) the obvious.
		if len(enabled_names) == 1:
			only = enabled_names[0]
			if issuer != only:
				self.payment_channel = only
				frappe.msgprint(
					_("Control numbers will be issued by {0} (the only enabled channel).").format(only),
					alert=True,
					indicator="blue",
				)
			self._validate_issuer_can_mint(self.payment_channel)
			return

		if not issuer:
			frappe.throw(_("Select which channel issues the control numbers ('Control Numbers Issued By')."))
		if not enabled.get(issuer):
			frappe.throw(
				_(
					"Control numbers cannot be issued by {0} because it is not ticked as a "
					"collection channel on this project. Tick it, or choose a ticked channel as issuer."
				).format(issuer)
			)
		self._validate_issuer_can_mint(issuer)

	# Channels that actually have a control-number generation block today.
	# Malipopay's is DELIBERATELY absent (operator decision 2026-09-05): until a
	# project needs Malipopay as issuer, that code stays unwritten — so an LA
	# must not be savable in a shape that names an issuer with no minting code.
	_CHANNELS_WITH_GENERATION = ("TCB",)

	def _validate_issuer_can_mint(self, issuer):
		if issuer and issuer not in self._CHANNELS_WITH_GENERATION:
			frappe.throw(
				_(
					"{0} cannot issue control numbers yet — its number-generation "
					"block is not built. Keep TCB ticked as the issuing channel "
					"(other channels can still collect alongside it)."
				).format(issuer)
			)

	def before_update_after_submit(self):
		"""Ticking a collection channel on a submitted project (operator, 14 Oct
		2026: the live project is submitted, so Malipopay could not be ticked).

		The form refreshes the cost figures and the period summary when it opens;
		those must not block the save. Frappe skips validate() on this save, so the
		channel rules are checked here. The issuer itself stays locked."""
		drop_rounding_noise_on_locked_fields(self)
		# a display line rebuilt by the form from fields locked after submit
		self.payment_period_summary = frappe.db.get_value(
			"Land Acquisition", self.name, "payment_period_summary"
		)
		issuer = (self.get("payment_channel") or "").strip()
		ticked = {
			"TCB": cint(self.get("collect_via_tcb")),
			"Malipopay": cint(self.get("collect_via_malipopay")),
		}
		if issuer and not ticked.get(issuer):
			frappe.throw(
				_(
					"{0} issues this project's control numbers, so it must stay ticked. Nothing was changed."
				).format(issuer)
			)
		self._guard_malipopay_switch()
		self._validate_channel_roles()

	def _guard_malipopay_switch(self):
		"""The Malipopay box on a submitted project (operator, 16 Oct 2026).

		Only a System Manager may change it. It cannot be unticked while any of the
		project's Malipopay bills is not yet ended: those customers can still pay on
		the bill, but their orders would lose the nightly money check and the
		reporting of their bank payments to Malipopay."""
		before = self.get_doc_before_save()
		if not before or cint(before.get("collect_via_malipopay")) == cint(self.get("collect_via_malipopay")):
			return
		if "System Manager" not in frappe.get_roles():
			frappe.throw(
				_(
					"Only a System Manager can change Collect via Malipopay on a submitted project. Nothing was changed."
				),
				frappe.PermissionError,
			)
		if cint(self.get("collect_via_malipopay")):
			return
		open_bills = self._open_malipopay_bills()
		if open_bills:
			frappe.throw(
				_(
					"Collect via Malipopay cannot be switched off: this project still has {0} Malipopay bill(s) "
					"that are not ended. End each one first (close the order for good, terminate or cancel it), "
					"then untick. Nothing was changed."
				).format(open_bills)
			)

	def _open_malipopay_bills(self):
		if not frappe.db.table_exists("Malipopay Collection"):
			return 0
		return frappe.db.count(
			"Malipopay Collection", {"land_acquisition": self.name, "state": ["!=", "Dead"]}
		)

	def before_submit(self):
		if self.status != "Approved":
			frappe.throw(_("Land Acquisition must be Approved through the workflow before submission."))
		self.approval_date = today()
		self.approved_by = frappe.session.user

	def on_submit(self):
		sync_land_acquisition_cost_summary(self.name)
		sync_land_acquisition_plot_summary(self.name)

	def before_cancel(self):
		self._block_if_active_plots()
		# Ledger rows carry the project too; skip them so Frappe's link check
		# names the invoice, journal or payment behind them.
		self.ignore_linked_doctypes = ("GL Entry", "Payment Ledger Entry")

	def _block_if_active_plots(self):
		if not frappe.db.exists("DocType", "Plot Master"):
			return
		active_plots = frappe.db.count("Plot Master", {"land_acquisition": self.name, "docstatus": 1})
		if not active_plots:
			return
		sample = frappe.db.get_all(
			"Plot Master",
			filters={"land_acquisition": self.name, "docstatus": 1},
			fields=["name"],
			limit_page_length=3,
		)
		names = ", ".join(r.name for r in sample)
		extra = f" and {active_plots - len(sample)} more" if active_plots > len(sample) else ""
		frappe.throw(
			_(
				"Cannot cancel — submitted plots still exist: {0}{1}. Cancel those Plot Master records first."
			).format(names, extra)
		)

	def on_cancel(self):
		self.db_set("status", "Cancelled")
		sync_land_acquisition_cost_summary(self.name)
		sync_land_acquisition_plot_summary(self.name)

	# ── Private validators ───────────────────────────────────────────────────

	def _validate_area(self):
		if not self.total_area_sqm or flt(self.total_area_sqm) <= 0:
			frappe.throw(_("Total Area must be greater than zero."))

	def _validate_coordinates(self):
		has_lat = self.latitude not in (None, "")
		has_lng = self.longitude not in (None, "")
		if has_lat != has_lng:
			frappe.throw(_("Enter both Latitude and Longitude, or leave both blank."))
		if has_lat:
			lat = flt(self.latitude)
			lng = flt(self.longitude)
			if lat < -90 or lat > 90:
				frappe.throw(_("Latitude must be between -90 and 90."))
			if lng < -180 or lng > 180:
				frappe.throw(_("Longitude must be between -180 and 180."))

	def _convert_payment_period_to_days(self):
		total = (
			cint(self.payment_years or 0) * 365
			+ cint(self.payment_months or 0) * 30
			+ cint(self.payment_days_input or 0)
		)
		if total <= 0:
			frappe.throw(_("Payment period must be greater than zero."))
		self.payment_completion_days = total
		self.payment_period_summary = _build_period_summary(
			cint(self.payment_years or 0),
			cint(self.payment_months or 0),
			cint(self.payment_days_input or 0),
			total,
		)

	def _validate_sales_defaults(self):
		if not (0 <= flt(self.booking_fee_percent) <= 100):
			frappe.throw(_("Booking Fee % must be between 0 and 100."))
		if not (0 <= flt(self.government_share_percent) <= 100):
			frappe.throw(_("Government Share % must be between 0 and 100."))
		if cint(self.payment_completion_days) <= 0:
			frappe.throw(_("Payment Completion Days must be greater than zero."))

	def _validate_and_set_tcb_patterns(self):
		# TCB credentials/patterns only apply when TCB is an enabled channel.
		if not cint(self.get("collect_via_tcb")):
			return
		if not self.partner_code:
			frappe.throw(_("Partner Code is required."))

		if "-" not in self.partner_code:
			frappe.throw(
				_(
					"Partner Code must contain a hyphen (e.g., 'PART-GVA') to generate the TCB Control Number pattern."
				)
			)

		suffix = self.partner_code.split("-")[-1].strip()
		if not suffix:
			frappe.throw(_("Partner Code suffix cannot be empty."))

		self.control_number_pattern = f"99910#{suffix}####"
		self.related_control_number_pattern = f"9992#R{suffix}####"

	def _validate_plot_type_rates(self):
		seen = set()
		for row in self.get("plot_type_rates") or []:
			if not row.plot_type:
				frappe.throw(f"Row {row.idx} in Plot Type Rates is missing a Plot Type.")
			if row.plot_type in seen:
				frappe.throw(
					f"Plot Type '{row.plot_type}' appears more than once in the rates table. "
					"Each plot type can only have one rate per Land Acquisition."
				)
			seen.add(row.plot_type)


# =============================================================================
# Plot summary — counts of submitted Plot Masters per status
# =============================================================================


@frappe.whitelist(methods=["POST"])
def refresh_plot_summary(land_acquisition: str):
	"""The project form's plot counts: recount, save and return them.

	Answers anyone who may open the project and refuses everyone else.
	"""
	frappe.has_permission("Land Acquisition", "read", land_acquisition, throw=True)
	return sync_land_acquisition_plot_summary(land_acquisition)


def sync_land_acquisition_plot_summary(land_acquisition):
	"""Recount the project's plots by status and save the counts on it.

	Not a web door: plot, application, contract and handover code call it,
	including for bank callbacks and users with no rights on the project.
	The form goes through refresh_plot_summary.
	"""
	if not land_acquisition or not frappe.db.exists("Land Acquisition", land_acquisition):
		return {}

	# Plot Master is created in Phase 3 — skip until then
	if not frappe.db.exists("DocType", "Plot Master"):
		return {
			"total_plots": 0,
			"available_plots": 0,
			"reserved_plots": 0,
			"delivered_plots": 0,
		}

	counts = frappe.db.sql(
		"""
        SELECT
            COUNT(*) AS total,
            SUM(status = 'Available') AS available,
            SUM(status IN ('Pending Fee', 'Pending Advance', 'Reserved', 'Ready for Handover')) AS reserved,
            SUM(status IN ('Delivered', 'Title Closed')) AS delivered
        FROM `tabPlot Master`
        WHERE land_acquisition = %s AND docstatus = 1
    """,
		land_acquisition,
		as_dict=True,
	)[0]

	total = int(counts.total or 0)
	available = int(counts.available or 0)
	reserved = int(counts.reserved or 0)
	delivered = int(counts.delivered or 0)

	frappe.db.set_value(
		"Land Acquisition",
		land_acquisition,
		{
			"total_plots": total,
			"available_plots": available,
			"reserved_plots": reserved,
			"delivered_plots": delivered,
		},
		update_modified=False,
	)

	current_status = frappe.db.get_value("Land Acquisition", land_acquisition, "status")
	if current_status in ("Approved", "Subdivided"):
		new_status = "Subdivided" if total > 0 else "Approved"
		if new_status != current_status:
			frappe.db.set_value(
				"Land Acquisition", land_acquisition, "status", new_status, update_modified=False
			)

	return {
		"total_plots": total,
		"available_plots": available,
		"reserved_plots": reserved,
		"delivered_plots": delivered,
	}


def _build_period_summary(years, months, days, total_days):
	parts = []
	if years:
		parts.append(f"{years} year{'s' if years > 1 else ''}")
	if months:
		parts.append(f"{months} month{'s' if months > 1 else ''}")
	if days:
		parts.append(f"{days} day{'s' if days > 1 else ''}")
	label = " + ".join(parts) if parts else "0 days"
	return f"{label} = {total_days} days total"
