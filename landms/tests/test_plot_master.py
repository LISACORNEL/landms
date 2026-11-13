from frappe.tests.utils import FrappeTestCase

from landms.landms.doctype.plot_master.plot_master import _build_plot_stock_entry_doc


class TestPlotMasterStockEntryAccounts(FrappeTestCase):
	def test_plot_stock_entry_uses_asset_account_not_item_cogs_default(self):
		doc = _build_plot_stock_entry_doc(
			company="LAND ACQUISITION",
			plot_number="A-001",
			land_acquisition="LACQ-TEST-0001",
			item_code="RESIDENTIAL PLOT",
			basic_rate=1000,
			target_warehouse="Plot Inventory Warehouse - LA",
			serial_number="PLT-TEST-0001",
			difference_account="1411 - Land Under Development - LA",
			plot="PLT-TEST-0001",
		)

		self.assertEqual(doc["difference_account"], "1411 - Land Under Development - LA")
		self.assertEqual(len(doc["items"]), 1)
		self.assertEqual(doc["items"][0]["expense_account"], "1411 - Land Under Development - LA")
