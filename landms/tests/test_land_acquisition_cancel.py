"""Cancelling a project cancels the project and nothing else (LA-1).

Before LA-1 a project's cancel cancelled its unpaid supplier invoices and
purchase orders by itself, then blanked the project on 18 kinds of records,
ledger rows included. Now Frappe's own link check refuses the cancel while a
submitted invoice, journal or order still belongs to the project, and nothing
is blanked: a project cancelled after its records keeps its name on them.

Safety (operator, 5 Nov 2026: "we don't want to break the production data"):
every write is rolled back, and a database commit or an outside call (bank,
SMS, email gateway) fails the test instead of happening.
"""

from unittest.mock import patch

import frappe
from frappe.model.workflow import apply_workflow, get_transitions, get_workflow_name
from frappe.tests.utils import FrappeTestCase
from frappe.utils import today

from landms.landms.doctype.landms_project_setting.landms_project_setting import copy_of_landms_settings

# The way forward through the approval workflows, in the order users click.
_FORWARD = ("Submit for Approval", "Submit", "Approve")
# Records that can carry the project; only those with the field on this site are counted.
_TAGGED = ("Purchase Invoice Item", "Journal Entry Account", "GL Entry", "Payment Ledger Entry")


def _no_commit():
	raise AssertionError("a database commit happened inside the LA-1 test; nothing may be saved for real")


def _no_outside_call(*args, **kwargs):
	raise AssertionError("an outside call was attempted inside the LA-1 test")


class TestLandAcquisitionCancel(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=_no_outside_call)
		blocker.start()
		self.addCleanup(blocker.stop)

		self.company = frappe.db.get_value("Land Acquisition", {"docstatus": 1}, "company") or self._pick(
			"Company", {}
		)
		self.cost_center = frappe.get_cached_value("Company", self.company, "cost_center") or self._pick(
			"Cost Center", {"company": self.company, "is_group": 0, "disabled": 0}
		)
		self.debit_account = self._plain_account("Asset")
		self.credit_account = self._plain_account("Liability")
		self.supplier = self._pick("Supplier", {"disabled": 0, "on_hold": 0})
		self.item = self._pick(
			"Item", {"is_stock_item": 0, "is_purchase_item": 1, "disabled": 0, "has_variants": 0}
		)

	def _plain_account(self, root_type):
		"""An ordinary ledger account that no site script rewrites.

		Live re-tags every journal line on its land account to the live project
		(Server Script "Set Chamwino Land Acquisition on JE Account"), so a line
		there would never belong to the test project.
		"""
		scripted = " ".join(frappe.get_all("Server Script", filters={"disabled": 0}, pluck="script"))
		accounts = frappe.get_all(
			"Account",
			filters={
				"company": self.company,
				"root_type": root_type,
				"is_group": 0,
				"disabled": 0,
				"account_type": ["is", "not set"],
			},
			pluck="name",
			order_by="name",
		)
		for account in accounts:
			if account not in scripted:
				return account
		self.skipTest(f"this site has no plain {root_type} account")

	def _pick(self, doctype, filters):
		name = frappe.db.get_value(doctype, filters, "name")
		if not name:
			self.skipTest(f"this site has no {doctype} matching {filters}")
		return name

	def _submit(self, doc):
		"""Submit the way users do: through the doctype's workflow when it has one."""
		if not get_workflow_name(doc.doctype):
			doc.submit()
			return doc
		for _ in range(5):
			doc.reload()
			if doc.docstatus == 1:
				return doc
			actions = [t.action for t in get_transitions(doc)]
			action = next((a for a in _FORWARD if a in actions), None)
			if not action:
				self.fail(
					f"{doc.doctype} {doc.name}: no way forward through its workflow (offered {actions})"
				)
			apply_workflow(doc, action)
		self.fail(f"{doc.doctype} {doc.name} was not submitted through its workflow")

	def _project(self):
		project = frappe.get_doc(
			{
				"doctype": "Land Acquisition",
				"acquisition_name": "LA-1 test " + frappe.generate_hash(length=6),
				"acquisition_date": today(),
				"company": self.company,
				"region": "Dodoma",
				"district": "Test",
				"total_area_sqm": 1000,
				"land_use_type": "Residential",
				"booking_fee_percent": 60,
				"government_share_percent": 28.5,
				"payment_completion_days": 90,
				"collect_via_tcb": 1,
				"payment_channel": "TCB",
				"partner_code": "PART-T" + frappe.generate_hash(length=3).upper(),
			}
		).insert()
		# a project is approved only once its setting record is filled (LA-7)
		setting = frappe.get_doc("LandMS Project Setting", project.name)
		setting.update(copy_of_landms_settings(self.company)).save(ignore_permissions=True)
		return self._submit(project)

	def _supplier_invoice(self, project):
		invoice = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": self.company,
				"supplier": self.supplier,
				"posting_date": today(),
				"due_date": today(),
				"bill_no": "LA-1-TEST-" + frappe.generate_hash(length=6),
				"cost_center": self.cost_center,
				"items": [
					{
						"item_code": self.item,
						"qty": 1,
						"rate": 1000,
						"cost_center": self.cost_center,
						"land_acquisition": project.name,
					}
				],
			}
		).insert()
		return self._submit(invoice)

	def _journal(self, project):
		journal = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"voucher_type": "Journal Entry",
				"company": self.company,
				"posting_date": today(),
				"user_remark": "LA-1 test: a land cost belonging to the project",
				"accounts": [
					{
						"account": self.debit_account,
						"debit_in_account_currency": 500,
						"cost_center": self.cost_center,
						"land_acquisition": project.name,
					},
					{
						"account": self.credit_account,
						"credit_in_account_currency": 500,
						"cost_center": self.cost_center,
					},
				],
			}
		).insert()
		return self._submit(journal)

	def _cancel_as_one_request(self, project):
		"""A refused cancel undoes its own writes, the way a web request does."""
		frappe.db.savepoint("la1_cancel")
		try:
			frappe.get_doc("Land Acquisition", project.name).cancel()
		except Exception:
			frappe.db.rollback(save_point="la1_cancel")
			raise

	def _docstatus(self, doctype, name):
		return frappe.db.get_value(doctype, name, "docstatus")

	def _rows_carrying(self, project):
		return {
			doctype: frappe.db.count(doctype, {"land_acquisition": project})
			for doctype in _TAGGED
			if frappe.get_meta(doctype).has_field("land_acquisition")
		}

	def _assert_ledger_carried_the_project(self, before):
		"""Where ledger rows carry the project (live), there must have been some to keep."""
		if "GL Entry" in before:
			self.assertGreater(
				before["GL Entry"], 0, "no ledger row carried the project; keeping none proves nothing"
			)

	def test_a_supplier_invoice_stops_the_cancel_and_is_left_alone(self):
		project = self._project()
		invoice = self._supplier_invoice(project)
		before = self._rows_carrying(project.name)

		with self.assertRaisesRegex(frappe.LinkExistsError, invoice.name):
			self._cancel_as_one_request(project)

		self.assertEqual(self._docstatus("Purchase Invoice", invoice.name), 1)
		self.assertEqual(self._docstatus("Land Acquisition", project.name), 1)
		self.assertEqual(self._rows_carrying(project.name), before)
		self.assertEqual(before["Purchase Invoice Item"], 1)
		self._assert_ledger_carried_the_project(before)

	def test_a_journal_entry_stops_the_cancel_and_keeps_the_project(self):
		project = self._project()
		journal = self._journal(project)
		before = self._rows_carrying(project.name)

		with self.assertRaisesRegex(frappe.LinkExistsError, journal.name):
			self._cancel_as_one_request(project)

		self.assertEqual(self._docstatus("Journal Entry", journal.name), 1)
		self.assertEqual(self._docstatus("Land Acquisition", project.name), 1)
		self.assertEqual(self._rows_carrying(project.name), before)
		self.assertEqual(before["Journal Entry Account"], 1)
		self._assert_ledger_carried_the_project(before)

	def test_once_its_records_are_cancelled_the_project_cancels_and_they_keep_its_name(self):
		"""What "Cancel All" does: the person cancels the records first, then the project."""
		project = self._project()
		invoice = self._supplier_invoice(project)
		journal = self._journal(project)
		frappe.get_doc("Purchase Invoice", invoice.name).cancel()
		frappe.get_doc("Journal Entry", journal.name).cancel()
		before = self._rows_carrying(project.name)

		frappe.get_doc("Land Acquisition", project.name).cancel()

		self.assertEqual(self._docstatus("Land Acquisition", project.name), 2)
		self.assertEqual(self._rows_carrying(project.name), before)
		self.assertEqual(before["Purchase Invoice Item"], 1)
		self.assertEqual(before["Journal Entry Account"], 1)
		self._assert_ledger_carried_the_project(before)

	def test_a_project_with_nothing_booked_cancels_normally(self):
		project = self._project()

		frappe.get_doc("Land Acquisition", project.name).cancel()

		self.assertEqual(self._docstatus("Land Acquisition", project.name), 2)
