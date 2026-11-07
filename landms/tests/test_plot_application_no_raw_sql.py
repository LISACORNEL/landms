"""The plot application's plot lock without raw SQL (PA-4).

Before submitting an application, and before recording its fee as paid, the plot's row is locked
so two applications cannot take the same plot at the same moment. The lock is now Frappe's own
row lock (SELECT ... FOR UPDATE through get_value) instead of a raw SQL string. On a copy of live,
two separate processes locking the same plot behaved identically before and after: the second
waited exactly as long as the first held the plot.

Safety (operator, 5 Nov 2026: "we don't want to break the production data"): every write is
rolled back, and a database commit or an outside call (bank, SMS, email gateway) fails the test.
"""

import re
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase


def _no_commit():
	raise AssertionError("a database commit happened inside the PA-4 test; nothing may be saved for real")


def _no_outside_call(*args, **kwargs):
	raise AssertionError("an outside call was attempted inside the PA-4 test")


class TestPlotApplicationNoRawSql(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=_no_outside_call)
		blocker.start()
		self.addCleanup(blocker.stop)

	def test_no_raw_sql_in_the_application_code(self):
		source = open(
			frappe.get_app_path("landms", "landms", "doctype", "plot_application", "plot_application.py")
		).read()
		self.assertFalse(re.search(r"frappe\.db\.(sql|multisql)\b", source))

	def test_the_plot_lock_is_a_row_lock_on_the_plot(self):
		plot = frappe.db.get_value("Plot Master", {"docstatus": 1}, "name")
		if not plot:
			self.skipTest("this site has no plot")
		application = frappe.new_doc("Plot Application")
		application.plot = plot

		with patch.object(frappe.db, "get_value", wraps=frappe.db.get_value) as get_value:
			application._lock_plot_row()

		get_value.assert_called_once_with("Plot Master", plot, "name", for_update=True)
		query = frappe.db.get_values("Plot Master", plot, "name", for_update=True, run=False)
		self.assertIn("FOR UPDATE", str(query).upper())

	def test_no_plot_no_lock(self):
		application = frappe.new_doc("Plot Application")

		with patch.object(frappe.db, "get_value", wraps=frappe.db.get_value) as get_value:
			application._lock_plot_row()

		get_value.assert_not_called()
