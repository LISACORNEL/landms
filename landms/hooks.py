app_name = "landms"
app_title = "Landms"
app_publisher = "Aakvatech Limited"
app_description = "Land Management System"
app_email = "info@aakvatech.com"
app_license = "mit"

required_apps = ["erpnext"]

doctype_js = {
	"Sales Order": "public/js/sales_order.js",
	"Purchase Order": "public/js/purchase_order.js",
	"Purchase Invoice": "public/js/purchase_invoice.js",
}

after_install = "landms.install.after_install"

# Receipt verification (TRA-style QR on the plot handover receipt)
jinja = {
	"methods": ["landms.receipt_verification.landms_receipt_verification"],
}
website_route_rules = [
	{"from_route": "/verify/<code>", "to_route": "verify"},
]

after_migrate = [
	"landms.patches.custom_fields.create_custom_fields.execute",
	"landms.patches.property_setter.create_property_setters.execute",
]

doc_events = {
	"Purchase Order": {
		"validate": "landms.landms.doctype.land_acquisition.land_acquisition.set_land_acquisition_expense_account",
		"on_submit": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_order",
		"on_cancel": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_order",
		"on_update_after_submit": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_order",
	},
	"Purchase Invoice": {
		"validate": "landms.landms.doctype.land_acquisition.land_acquisition.set_land_acquisition_expense_account",
		"on_submit": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_invoice",
		"on_cancel": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_invoice",
		"on_update_after_submit": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_invoice",
	},
	# Cost centre: one rule for every LandMS document (landms/cost_center.py) —
	# fills EMPTY header/line cost centres from LandMS Settings, only on
	# documents that are provably LandMS's and belong to the LandMS company.
	# before_validate runs on every save path and ahead of the mandatory check.
	"Delivery Note": {
		"before_validate": "landms.cost_center.apply_landms_cost_center",
	},
	"Journal Entry": {
		"before_validate": "landms.cost_center.apply_landms_cost_center",
		"on_submit": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_journal_entry",
		"on_cancel": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_journal_entry",
	},
	"Payment Entry": {
		"before_validate": "landms.cost_center.apply_landms_cost_center",
		"validate": [
			"landms.landms.doctype.land_acquisition.land_acquisition.autoset_land_acquisition_on_payment_entry",
			"landms.payment_sync.validate_payment_entry",
		],
		"on_submit": [
			"landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_payment_entry",
			"landms.payment_sync.on_submit_payment_entry",
		],
		"on_cancel": [
			"landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_payment_entry",
			"landms.payment_sync.on_cancel_payment_entry",
			# Malipopay section 11: a DEBIT may be due; inert unless
			# Malipopay Settings.external_entries_mode is on, never raises
			"landms.malipopay_external.on_payment_entry_cancel",
		],
	},
	# Malipopay section 11 triggers — both inert unless external_entries_mode
	# is on, both return at once for anything that is not a Payment Entry
	# allocated to a plot invoice/order, and neither can raise into the posting.
	"Payment Ledger Entry": {
		"on_submit": [
			"landms.malipopay_external.on_payment_ledger_entry_submit",
			# a payment moved onto a plot order by Payment Reconciliation: refresh
			# order, invoice lines and contract after commit (no PE event fires)
			"landms.reallocation_sync.on_payment_ledger_entry_submit",
		],
	},
	"Unreconcile Payment": {
		"on_submit": [
			"landms.malipopay_external.on_unreconcile_payment_submit",
			"landms.reallocation_sync.on_unreconcile_payment_submit",
		],
	},
	"Sales Invoice": {
		"before_validate": "landms.cost_center.apply_landms_cost_center",
		"before_save": "landms.sales_invoice_hooks.before_save_sales_invoice",
	},
	"Sales Order": {
		"before_validate": "landms.cost_center.apply_landms_cost_center",
		"validate": "landms.sales_order_hooks.validate_sales_order",
		"before_submit": "landms.sales_order_hooks.before_submit_sales_order",
		"on_submit": [
			"landms.sales_order_hooks.submit_sales_order",
			# dependent-rail wiring: stages a Malipopay Collection AFTER the
			# issuer-side work; guarded, never blocks, HTTP only post-commit
			"landms.malipopay.on_sales_order_submit",
		],
		"before_cancel": "landms.sales_order_hooks.before_cancel_sales_order",
		"on_cancel": [
			"landms.sales_order_hooks.cancel_sales_order",
			"landms.malipopay.on_sales_order_cancel",
		],
		# ERPNext's Status > Re-open: refused on a terminated plot order, and on
		# an order closed for good at Malipopay
		"before_change": [
			"landms.sales_order_hooks.before_change_sales_order",
			"landms.malipopay.before_change_sales_order",
		],
	},
}

scheduler_events = {
	# hourly in the long queue (25 min, not 5): each bill can mean a call to Malipopay
	"hourly_long": [
		# Malipopay delivers once with no retry: anything the worker left
		# Pending/Failed in the inbox is re-booked from the breakdown receipts
		"landms.malipopay.process_pending_webhook_events",
		# bills follow their order: expire/pause stopped orders, pause fully paid
		# ones, resume ones that owe again; retry every move that has not landed
		"landms.malipopay.kill_pending_collections",
	],
	"daily_long": [
		"landms.tasks.run_daily_tcb_reconciliation",
	],
	"cron": {
		# the nightly expiry / overdue run at 01:00, after the TCB reconciliation;
		# it runs in the long queue (25 min, not 5) and hands anything left to a
		# follow-up job before it would be cut off
		"0 1 * * *": ["landms.tasks.start_nightly_run"],
		# 04:40 — the daily catch-all for Malipopay money whose notice never
		# arrived (Malipopay does not resend). It runs AFTER the 01:00 nightly
		# run, so that run reads each order's Malipopay bill itself before it
		# stops the order (tasks._malipopay_money_waiting).
		"40 4 * * *": [
			# hands the sweep to the long queue (a timed job alone gets 5 minutes)
			"landms.malipopay_sweep.start_daily_malipopay_sweep",
		],
		# 05:10 — after the sweep has booked any Malipopay receipts, bring each
		# live bill's "paid outside Malipopay" figure in line (inert when Off)
		"10 5 * * *": [
			# hands the pass to the long queue (a timed job alone gets 5 minutes)
			"landms.malipopay_external.start_external_entries_sync",
		],
		# every three hours at half past, 03:30 to 21:30 — never at midnight (the
		# nightly jobs' window): finish the steps queued under Waiting for TCB.
		# Only new events since the Malipopay go-live are ever queued; each step
		# is checked again before anything is sent; does nothing unless TCB
		# Integration Settings "Automatic Catch-up" is ticked.
		"30 3-21/3 * * *": [
			"landms.payments.tcb_catch_up.process_queue",
		],
	},
}
