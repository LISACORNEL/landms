"""The project code, split into one file per job (LA-2), behaves exactly as before.

land_acquisition.py keeps the form (checks, approval, cancel, plot and period summaries);
cost_totals.py adds up what the land has cost; recalculate_plot_costs.py is the Recalculate
Plot Costs button; landms/overrides/update_project_costs.py holds the rules that run when a
purchase, journal or payment is saved. Every hook and button points at real code, the moved
buttons still answer at their old address, and a project still saves, approves and cancels.

Safety (operator, 5 Nov 2026: "we don't want to break the production data"): every write is
rolled back, and a database commit or an outside call (bank, SMS, email gateway) fails the test.
"""

import importlib
import os
import re
from unittest.mock import patch

import frappe
from frappe.model.workflow import apply_workflow, get_transitions, get_workflow_name
from frappe.tests.utils import FrappeTestCase
from frappe.utils import today

from landms.landms.doctype.land_acquisition import cost_totals, recalculate_plot_costs
from landms.landms.doctype.land_acquisition import land_acquisition as form
from landms.overrides import update_project_costs

_FORWARD = ("Submit for Approval", "Submit", "Approve")
_RULES_ON_OTHER_DOCUMENTS = {
	"set_land_acquisition_expense_account",
	"sync_costs_from_purchase_order",
	"sync_costs_from_purchase_invoice",
	"sync_costs_from_journal_entry",
	"autoset_land_acquisition_on_payment_entry",
	"sync_costs_from_payment_entry",
}


def _no_commit():
	raise AssertionError("a database commit happened inside the LA-2 test; nothing may be saved for real")


def _no_outside_call(*args, **kwargs):
	raise AssertionError("an outside call was attempted inside the LA-2 test")


def _resolve(address):
	"""The object a dotted text address names: a module, a function, or a method inside a class."""
	parts = address.split(".")
	for cut in range(len(parts), 0, -1):
		try:
			target = importlib.import_module(".".join(parts[:cut]))
		except ImportError:
			continue
		for name in parts[cut:]:
			target = getattr(target, name)
		return target
	raise ImportError(address)


def _defined_in(module):
	"""Names of the functions and classes a module defines itself (not those it imports)."""
	return {
		name
		for name, value in vars(module).items()
		if callable(value) and getattr(value, "__module__", None) == module.__name__
	}


class TestLandAcquisitionSplit(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=_no_outside_call)
		blocker.start()
		self.addCleanup(blocker.stop)

	def test_every_landms_hook_points_at_real_code(self):
		for doctype, events in frappe.get_module("landms.hooks").doc_events.items():
			for event, paths in events.items():
				for path in [paths] if isinstance(paths, str) else paths:
					self.assertTrue(callable(frappe.get_attr(path)), f"{doctype}.{event}: {path}")

	def test_every_function_address_written_as_text_is_real(self):
		"""Jobs, hooks, buttons and logs name functions as text, which no linter checks: a moved
		function with a stale text address only fails when that path runs (found in LA-2: the
		Recalculate job). Every "landms.x.y" text address in the app must name real code."""
		root = frappe.get_app_path("landms")
		address = re.compile(r"""["'](landms(?:\.[A-Za-z_][A-Za-z0-9_]*){2,})["']""")
		stale = []
		for folder, _dirs, files in os.walk(root):
			if os.sep + "tests" in folder or os.sep + "public" in folder:
				continue
			for name in files:
				if not name.endswith((".py", ".js", ".json")):
					continue
				path = os.path.join(folder, name)
				for found in address.finditer(open(path, errors="ignore").read()):
					try:
						_resolve(found.group(1))
					except (ImportError, AttributeError):
						stale.append(f"{os.path.relpath(path, root)}: {found.group(1)}")
		self.assertEqual(stale, [])

	def test_recalculate_queues_a_job_that_exists(self):
		project = frappe.db.get_value(
			"Land Acquisition", {"docstatus": 1, "recalculation_in_progress": 0}, "name"
		)
		if not project:
			self.skipTest("this site has no approved project")
		with patch.object(frappe.db, "commit"), patch("frappe.enqueue") as enqueue:
			recalculate_plot_costs.recalculate_plot_costs(project)

		job = enqueue.call_args.args[0]
		self.assertIs(frappe.get_attr(job), recalculate_plot_costs._run_plot_cost_recalculation)
		self.assertIs(
			frappe.get_attr(
				"landms.landms.doctype.land_acquisition.land_acquisition._run_plot_cost_recalculation"
			),
			recalculate_plot_costs._run_plot_cost_recalculation,
			"a job queued before an update still finds its function",
		)

	def test_the_form_buttons_answer_post_only(self):
		source = open(
			frappe.get_app_path("landms", "landms", "doctype", "land_acquisition", "land_acquisition.js")
		).read()
		paths = set(re.findall(r'"(landms\.landms\.doctype\.land_acquisition\.[\w.]+)"', source))
		self.assertEqual(len(paths), 3, paths)
		for path in paths:
			method = frappe.get_attr(path)
			self.assertIn(method, frappe.whitelisted, path)
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["POST"], path)

	def test_moved_buttons_still_answer_at_their_old_address(self):
		self.assertIs(form.refresh_cost_summary, cost_totals.refresh_cost_summary)
		self.assertIs(form.get_land_acquisition_cost_summary, cost_totals.get_land_acquisition_cost_summary)
		self.assertIs(form.sync_land_acquisition_cost_summary, cost_totals.sync_land_acquisition_cost_summary)
		self.assertIs(form.recalculate_plot_costs, recalculate_plot_costs.recalculate_plot_costs)

	def test_each_file_holds_one_job(self):
		self.assertEqual(
			_defined_in(form),
			{
				"LandAcquisition",
				"refresh_plot_summary",
				"sync_land_acquisition_plot_summary",
				"_build_period_summary",
			},
		)
		self.assertEqual(
			_defined_in(recalculate_plot_costs), {"recalculate_plot_costs", "_run_plot_cost_recalculation"}
		)
		self.assertEqual(_defined_in(update_project_costs), _RULES_ON_OTHER_DOCUMENTS | {"_sync_many"})
		self.assertIn("get_land_acquisition_cost_summary", _defined_in(cost_totals))
		self.assertFalse(_defined_in(cost_totals) & _RULES_ON_OTHER_DOCUMENTS)

	def test_a_supplier_invoice_still_gets_the_land_account(self):
		land_account = frappe.db.get_single_value("LandMS Settings", "land_under_development_account")
		project = frappe.db.get_value("Land Acquisition", {"docstatus": 1}, "name")
		if not land_account or not project:
			self.skipTest("this site has no land account or no approved project")
		invoice = frappe.new_doc("Purchase Invoice")
		invoice.land_acquisition = project
		invoice.append("items", {"expense_account": "anything"})

		frappe.get_attr("landms.overrides.update_project_costs.set_land_acquisition_expense_account")(invoice)

		self.assertEqual(invoice.items[0].land_acquisition, project)
		self.assertEqual(invoice.items[0].expense_account, land_account)

	def _project(self):
		company = frappe.db.get_value("Land Acquisition", {"docstatus": 1}, "company") or frappe.db.get_value(
			"Company", {}, "name"
		)
		return frappe.get_doc(
			{
				"doctype": "Land Acquisition",
				"acquisition_name": "LA-2 test " + frappe.generate_hash(length=6),
				"acquisition_date": today(),
				"company": company,
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

	def _approve(self, doc):
		"""Approve the way users do; fill the project's settings first where that card (LA-7) is present."""
		if frappe.db.exists("DocType", "LandMS Project Settings") and frappe.db.exists(
			"LandMS Project Settings", doc.name
		):
			from landms.landms.doctype.landms_project_settings.landms_project_settings import (
				copy_of_landms_settings,
			)

			settings = frappe.get_doc("LandMS Project Settings", doc.name)
			settings.update(copy_of_landms_settings(doc.company))
			settings.save()
		if not get_workflow_name(doc.doctype):
			doc.submit()
			return doc
		for _ in range(5):
			doc.reload()
			if doc.docstatus == 1:
				return doc
			actions = [t.action for t in get_transitions(doc)]
			apply_workflow(doc, next(a for a in _FORWARD if a in actions))
		self.fail(f"{doc.name} was not approved")

	def test_a_project_still_saves_approves_and_cancels(self):
		project = self._approve(self._project())

		stored = frappe.db.get_value(
			"Land Acquisition",
			project.name,
			["docstatus", "total_plots", "acquisition_cost_tzs"],
			as_dict=True,
		)
		self.assertEqual(stored.docstatus, 1)
		self.assertEqual(stored.total_plots, 0, "the plot summary still runs on approval")
		self.assertEqual(stored.acquisition_cost_tzs, 0, "the cost summary still runs on approval")

		frappe.get_doc("Land Acquisition", project.name).cancel()
		self.assertEqual(frappe.db.get_value("Land Acquisition", project.name, "docstatus"), 2)
