"""Payment chain health report (PB-6): each broken link of a real payment chain shows up, one row each.

Every test breaks one link on real data inside the test's own transaction (rolled back), then runs
the report: a payment with no ledger rows, a payment whose government share journal is gone, an
order whose 'paid' disagrees with its invoice, a fully paid order whose contract is not Completed.
The report only reads; only System Managers may run it (Lisa, 5 Nov 2026).

Safety (operator, 5 Nov 2026: "we don't want to break the production data"): every write is rolled
back, and a database commit or an outside call (bank, SMS, email gateway) fails the test.
"""

import os
import re
from unittest.mock import patch

import frappe
from frappe.query_builder import DocType
from frappe.tests.utils import FrappeTestCase

from landms.landms.report.landms_payment_chain_health import landms_payment_chain_health as report

REPORT = "LandMS Payment Chain Health"


def _no_commit():
	raise AssertionError("a database commit happened inside the PB-6 test; nothing may be saved for real")


def _no_outside_call(*args, **kwargs):
	raise AssertionError("an outside call was attempted inside the PB-6 test")


class TestPaymentChainHealth(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=_no_outside_call)
		blocker.start()
		self.addCleanup(blocker.stop)

	def _rows(self, check, **filters):
		return [r for r in report.execute(filters)[1] if r["check"] == frappe._(check)]

	def _documents(self, check, **filters):
		return {r["document"] for r in self._rows(check, **filters)}

	def _pick(self, query, what):
		row = query.limit(1).run(as_dict=True)
		if not row:
			self.skipTest(f"this site has no {what}")
		return row[0]

	def test_a_payment_with_no_ledger_rows(self):
		pe, gl = DocType("Payment Entry"), DocType("GL Entry")
		payment = self._pick(
			frappe.qb.from_(pe)
			.join(gl)
			.on((gl.voucher_no == pe.name) & (gl.is_cancelled == 0))
			.select(pe.name)
			.where((pe.docstatus == 1) & (pe.land_acquisition.isnotnull()) & (pe.land_acquisition != "")),
			"plot payment with ledger rows",
		).name
		self.assertNotIn(payment, self._documents(report.NO_LEDGER))

		frappe.db.set_value("GL Entry", {"voucher_no": payment}, "is_cancelled", 1)

		self.assertIn(payment, self._documents(report.NO_LEDGER))

	def test_a_payment_whose_government_share_is_missing(self):
		pe, ref, si, je = (
			DocType("Payment Entry"),
			DocType("Payment Entry Reference"),
			DocType("Sales Invoice"),
			DocType("Journal Entry"),
		)
		la = DocType("Land Acquisition")
		found = self._pick(
			frappe.qb.from_(je)
			.join(pe)
			.on(pe.name == je.lms_payment_entry)
			.join(ref)
			.on(ref.parent == pe.name)
			.join(si)
			.on(si.name == ref.reference_name)
			.join(la)
			.on(la.name == si.land_acquisition)
			.select(pe.name.as_("payment"))
			.where(
				(je.docstatus == 1)
				& (pe.docstatus == 1)
				& (pe.payment_type == "Receive")
				& (si.is_plot_sale_invoice == 1)
				& (ref.allocated_amount > 0)
				& (la.government_share_percent > 0)
			),
			"plot payment with a government share journal",
		)
		self.assertNotIn(found.payment, self._documents(report.NO_SHARE))

		frappe.db.set_value("Journal Entry", {"lms_payment_entry": found.payment}, "lms_payment_entry", None)

		self.assertIn(found.payment, self._documents(report.NO_SHARE))

	def _open_plot_order(self, **where):
		so, si = DocType("Sales Order"), DocType("Sales Invoice")
		query = (
			frappe.qb.from_(so)
			.join(si)
			.on((si.name == so.plot_sales_invoice) & (si.docstatus == 1))
			.select(so.name, so.plot_contract, so.land_acquisition)
			.where((so.docstatus == 1) & so.status.notin(["Closed", "Cancelled"]))
		)
		if where.get("fully_paid"):
			query = query.where(si.outstanding_amount <= 0)
		for name in [r.name for r in query.limit(50).run(as_dict=True)]:
			if name not in self._documents(report.ORDER_VS_INVOICE) | self._documents(report.NOT_COMPLETED):
				return frappe._dict(
					frappe.db.get_value(
						"Sales Order", name, ["name", "plot_contract", "land_acquisition"], as_dict=True
					)
				)
		self.skipTest("this site has no healthy open plot order")

	def test_an_order_that_disagrees_with_its_invoice(self):
		order = self._open_plot_order()

		paid = frappe.db.get_value("Sales Order", order.name, "advance_paid")
		frappe.db.set_value("Sales Order", order.name, "advance_paid", paid + 1000, update_modified=False)

		self.assertIn(order.name, self._documents(report.ORDER_VS_INVOICE))

	def test_a_fully_paid_order_whose_contract_is_not_completed(self):
		order = self._open_plot_order(fully_paid=True)
		if not order.plot_contract:
			self.skipTest("the fully paid order has no contract")

		frappe.db.set_value(
			"Plot Contract", order.plot_contract, "contract_status", "Ongoing", update_modified=False
		)

		rows = self._rows(report.NOT_COMPLETED)
		self.assertIn(order.name, {r["document"] for r in rows})
		self.assertIn("Ongoing", next(r["detail"] for r in rows if r["document"] == order.name))

	def test_the_project_filter_narrows_the_rows(self):
		order = self._open_plot_order()
		frappe.db.set_value("Sales Order", order.name, "advance_paid", 1, update_modified=False)

		self.assertIn(
			order.name, self._documents(report.ORDER_VS_INVOICE, land_acquisition=order.land_acquisition)
		)
		self.assertNotIn(
			order.name, self._documents(report.ORDER_VS_INVOICE, land_acquisition="LACQ-NO-SUCH")
		)

	def test_one_card_per_check_green_at_zero(self):
		cards = report.execute({"land_acquisition": "LACQ-NO-SUCH"})[4]

		self.assertEqual(
			[c["label"] for c in cards],
			[
				frappe._(c)
				for c in (report.NO_LEDGER, report.NO_SHARE, report.ORDER_VS_INVOICE, report.NOT_COMPLETED)
			],
		)
		self.assertEqual({c["indicator"] for c in cards}, {"Green"})

	def test_it_only_reads(self):
		before = frappe.db.transaction_writes
		report.execute({})
		self.assertEqual(frappe.db.transaction_writes, before)

	def test_only_system_managers_may_run_it(self):
		roles = set(frappe.get_all("Has Role", {"parent": REPORT, "parenttype": "Report"}, pluck="role"))
		self.assertEqual(roles, {"System Manager"})

	def test_no_raw_sql(self):
		source = open(os.path.splitext(report.__file__)[0] + ".py").read()
		self.assertFalse(re.search(r"frappe\.db\.sql\b", source), "the report must use the query builder")
