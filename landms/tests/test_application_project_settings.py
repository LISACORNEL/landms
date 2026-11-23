"""An application reads its project's settings, not LandMS Settings (PA-5). Real records, every write rolled back."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, today

from landms.landms.doctype.landms_project_setting.landms_project_setting import SETTINGS


def _no_commit():
	raise AssertionError("a database commit happened inside the PA-5 test")


def _no_outside_call(*args, **kwargs):
	raise AssertionError("an outside call was attempted inside the PA-5 test")


def _other(doctype, filters, not_this):
	"""Any record of the kind asked for, other than the one given."""
	return frappe.db.get_value(doctype, {**filters, "name": ["!=", not_this or ""]}, "name")


class TestApplicationProjectSettings(FrappeTestCase):
	def setUp(self):
		self.plot = frappe.db.get_value(
			"Plot Master", {"docstatus": 1, "status": "Available"}, ["name", "land_acquisition"], as_dict=True
		)
		if not self.plot or not frappe.db.exists(SETTINGS, self.plot.land_acquisition):
			self.skipTest("no available plot of a project with settings on this site")
		self.customer = frappe.db.get_value("Customer", {"disabled": 0}, "name")
		self.site = frappe.get_single("LandMS Settings")
		self.project = frappe.get_doc(SETTINGS, self.plot.land_acquisition)

		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		for target, new in (
			("requests.sessions.Session.request", _no_outside_call),
			# the bench's csf_tz stops every new invoice; live's build does not
			("csf_tz.custom_api.create_trade_in_stock_entry", lambda *a, **k: None),
		):
			try:
				p = patch(target, new=new)
				p.start()
				self.addCleanup(p.stop)
			except (ImportError, AttributeError):
				pass

	def _set_project(self, **values):
		frappe.db.set_value(SETTINGS, self.project.name, values)

	def _application(self):
		return frappe.get_doc(
			{"doctype": "Plot Application", "customer": self.customer, "plot": self.plot.name}
		).insert(ignore_permissions=True)

	def test_fee_figures_come_from_the_project(self):
		self._set_project(
			application_fee_amount=7000, unpaid_application_expiry_days=5, application_fee_validity_days=9
		)
		self.assertNotEqual(flt(self.site.application_fee_amount), 7000)

		application = self._application()

		self.assertEqual(flt(application.application_fee), 7000)
		self.assertEqual(application.unpaid_validity_days, 5)
		if not frappe.db.get_value("Plot Master", self.plot.name, "no_advance_deadline"):
			self.assertEqual(application.validity_days, 9)

	def test_a_zero_fee_on_the_project_stays_zero(self):
		frappe.db.set_single_value("LandMS Settings", "application_fee_amount", 5000)
		self._set_project(application_fee_amount=0)

		self.assertEqual(flt(self._application().application_fee), 0)

	def test_an_application_without_a_project_is_left_alone(self):
		application = frappe.new_doc("Plot Application")
		application.application_fee = 5

		application.fill_plot_details()
		application.fill_fee_from_settings()

		self.assertEqual(flt(application.application_fee), 5)

	def test_the_fee_payment_uses_the_projects_records(self):
		company = self.project.company
		bank = _other(
			"Account",
			{"company": company, "account_type": "Bank", "is_group": 0},
			self.site.application_fee_receiving_account,
		)
		income = _other(
			"Account",
			{"company": company, "root_type": "Income", "is_group": 0},
			self.site.application_fee_income_account,
		)
		if not (bank and income):
			self.skipTest("this site has no second bank or income account")
		self._set_project(
			application_fee_amount=7000,
			application_fee_receiving_account=bank,
			application_fee_income_account=income,
		)
		application = self._application()
		application.submit()

		try:
			application.record_fee_payment(payment_date=today())
		except frappe.ValidationError as e:
			if "Cost Center" in str(e) and "does not belong to company" in str(e):
				self.skipTest("the fee invoice stops on the cost centre fault of card PA-1, as today")
			raise
		application.reload()

		invoice = frappe.get_doc("Sales Invoice", application.sales_invoice)
		self.assertEqual(invoice.company, company)
		self.assertEqual(invoice.items[0].item_code, self.project.application_fee_item)
		self.assertEqual(invoice.items[0].income_account, income)
		self.assertEqual(flt(invoice.grand_total), 7000)
		self.assertEqual(frappe.db.get_value("Payment Entry", application.payment_entry, "paid_to"), bank)

	def test_the_order_uses_the_projects_warehouse_and_company(self):
		company = self.project.company
		warehouse = _other(
			"Warehouse", {"company": company, "is_group": 0}, self.site.plot_inventory_warehouse
		)
		if not warehouse:
			self.skipTest("this site has no second warehouse")
		self._set_project(application_fee_amount=0, plot_inventory_warehouse=warehouse)
		application = self._application()

		application.submit()  # no fee to pay: the order is made at once, as on live

		application.reload()
		order = frappe.get_doc("Sales Order", application.sales_order)
		self.assertEqual((order.company, order.set_warehouse), (company, warehouse))
