# Copyright (c) 2026, Aakvatech and contributors
# For license information, please see license.txt

"""Receipt verification — the TRA-style QR on the plot handover receipt.

A submitted Plot Handover is GVA's receipt that the buyer has paid the plot in
full. Each one gets a random verification code; its QR opens
https://verify.gva.co.tz/verify/<code>, and that page asks verify_receipt(),
live, what our records say about the code right now.

Deliberately:
- the code is random, never the PHO number, so nobody can step through the
  sequence and collect every receipt;
- verify_receipt() is the only way in: read-only, POST, rate-limited, and it
  answers with one of three statuses: valid (with the receipt's details),
  cancelled (a real receipt since withdrawn — no details), or not_found;
- "valid" is decided on every check from the live records — the handover is
  still submitted AND its contract is still Completed — so a reversed payment
  or a cancelled handover stops verifying at once;
- every check is logged in Receipt Verification Log.
"""

import secrets

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, cstr, flt, fmt_money, formatdate

# Crockford base32: no I, L, O or U, so a code read aloud or typed from paper
# cannot be confused. 12 characters = 60 random bits.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 12
# The address printed in every QR is LandMS Settings > Receipt Verification
# Address (operator, 15 Oct 2026): set on the live site, not in the code. It
# can never change once receipts are out, so it is GVA's own domain pointed at
# this site, never the hosting address. While it is empty no QR is printed, so
# no receipt can carry an address that does not open the page yet.
ROUTE = "verify"


def new_code():
	for _attempt in range(10):
		code = "".join(secrets.choice(CODE_ALPHABET) for _i in range(CODE_LENGTH))
		if not frappe.db.exists("Plot Handover", {"verification_code": code}):
			return code
	frappe.throw("Could not generate a unique receipt verification code.")


def normalize_code(raw):
	"""The code as typed or scanned -> its stored form, or "" if it cannot be
	one. Accepts lower case, spaces and dashes, and the letters people confuse
	with digits (O for 0, I and L for 1)."""
	code = cstr(raw).upper().replace("-", "").replace(" ", "")
	code = code.translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))
	if len(code) != CODE_LENGTH or any(ch not in CODE_ALPHABET for ch in code):
		return ""
	return code


def display_code(code):
	return "-".join(code[i : i + 4] for i in range(0, len(code), 4))


def verify_page():
	"""The verify page address, or "" while LandMS Settings has none."""
	base = cstr(frappe.db.get_single_value("LandMS Settings", "receipt_verification_address"))
	base = base.strip().rstrip("/")
	return f"{base}/{ROUTE}" if base else ""


def verify_url(code):
	return f"{verify_page()}/{code}"


def assign_code(doc):
	"""Plot Handover before_submit: a fresh code on every submit. A corrected
	(amended) receipt gets its own code; the cancelled one keeps its old code,
	which now answers "cancelled".

	Never blocks the handover (operator, 15 Oct 2026: the QR is an extra, the
	sales team must always be able to issue): if no code can be made, the
	handover is issued without one, its receipt prints without a QR, and the
	problem is logged."""
	try:
		doc.verification_code = new_code()
	except Exception:
		doc.verification_code = None
		frappe.log_error(frappe.get_traceback(), "Receipt verification code not assigned")


# ---------------------------------------------------------------------------
# Print format
# ---------------------------------------------------------------------------


def landms_receipt_verification(doc):
	"""Jinja method for the handover print format:
	{code, display, url, page, qr} where qr is a PNG data URI and page is the
	address to type by hand, or {} when this receipt cannot be verified (a
	draft, a cancelled one, no code yet, or no verification address in
	LandMS Settings yet)."""
	try:
		code = cstr(doc.get("verification_code")) if doc else ""
		if not code or cint(doc.get("docstatus")) != 1 or not verify_page():
			return {}
		import pyqrcode

		url = verify_url(code)
		qr = pyqrcode.create(url, error="M")
		return {
			"code": code,
			"display": display_code(code),
			"url": url,
			"page": verify_page().split("://", 1)[-1],
			"qr": "data:image/png;base64," + qr.png_as_base64_str(scale=5, quiet_zone=2),
		}
	except Exception:
		# a QR problem must never stop a receipt from printing
		frappe.log_error(frappe.get_traceback(), "Receipt verification QR failed")
		return {}


# ---------------------------------------------------------------------------
# The read-only check
# ---------------------------------------------------------------------------


def money_owed(contract):
	"""What the contract's plot invoice still owes; None when there is no submitted invoice."""
	order, invoice = frappe.db.get_value(
		"Plot Contract", contract, ["sales_order", "booking_fee_invoice"]
	) or (None, None)
	invoice = (order and frappe.db.get_value("Sales Order", order, "plot_sales_invoice")) or invoice
	if not invoice or frappe.db.get_value("Sales Invoice", invoice, "docstatus") != 1:
		return None
	return flt(frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount"))


def judge(handover, contract_status, owed=0):
	"""(status, reason): status is valid / cancelled / not_found and goes to
	the caller; the reason is for our log only.

	cancelled = the code was genuinely issued but the receipt no longer
	stands: the handover was cancelled, or the contract is no longer fully
	paid (a reversed payment rolled back, a termination)."""
	if not handover or cint(handover.docstatus) == 0:
		return "not_found", "unknown code" if not handover else "handover not submitted"
	if cint(handover.docstatus) == 2:
		return "cancelled", "handover cancelled"
	if contract_status != "Completed":
		return "cancelled", f"contract is {contract_status or 'missing'}, not Completed"
	if owed is None or owed > 0:
		return "cancelled", "the plot invoice is not fully paid"
	return "valid", "valid"


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=30, seconds=5 * 60)
def verify_receipt(code=None):
	"""What GVA's records say about this receipt code, right now. Returns
	{"valid": True, "status": "valid", "receipt": {...}}, or
	{"valid": False, "status": "cancelled" | "not_found"} with no details."""
	entered = cstr(code).strip()[:64]
	normalized = normalize_code(entered)
	handover = None
	if normalized:
		handover = frappe.db.get_value(
			"Plot Handover",
			{"verification_code": normalized},
			[
				"name",
				"docstatus",
				"contract",
				"customer",
				"plot",
				"acquisition_name",
				"selling_price",
				"handover_date",
			],
			as_dict=True,
		)
	contract_status = (
		frappe.db.get_value("Plot Contract", handover.contract, "contract_status")
		if handover and handover.contract
		else None
	)
	owed = money_owed(handover.contract) if handover and handover.contract else None
	status, reason = judge(handover, contract_status, owed)
	_log(entered, handover, status, reason)
	if status != "valid":
		return {"valid": False, "status": status}

	holder = frappe.db.get_value("Customer", handover.customer, "customer_name") or handover.customer
	plot_number = frappe.db.get_value("Plot Master", handover.plot, "plot_number") or handover.plot
	return {
		"valid": True,
		"status": "valid",
		"receipt": {
			"receipt_no": handover.name,
			"holder": holder,
			"plot": plot_number,
			"project": handover.acquisition_name or "",
			"amount_paid": fmt_money(flt(handover.selling_price), precision=0, currency="TZS"),
			"handover_date": formatdate(handover.handover_date, "dd MMMM yyyy"),
			"code": display_code(normalized),
		},
	}


LOG_RESULT = {"valid": "Valid", "cancelled": "Cancelled", "not_found": "Not Valid"}


def _log(entered, handover, status, reason):
	"""Best-effort: a logging failure must never change the answer."""
	try:
		request = getattr(frappe.local, "request", None)
		frappe.get_doc(
			{
				"doctype": "Receipt Verification Log",
				"code_entered": entered,
				"plot_handover": handover.name if handover else None,
				"result": LOG_RESULT.get(status, "Not Valid"),
				"reason": reason,
				"ip_address": cstr(getattr(frappe.local, "request_ip", "") or "")[:140],
				"user_agent": cstr(request.headers.get("User-Agent") if request else "")[:500],
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Receipt verification log failed")
