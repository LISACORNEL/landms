"""One door for a plot's status, with a note on the plot's timeline (PM-1).

The rulebook must allow every move the app made before PM-1 (nothing that worked is
refused), refuse moves no code makes, leave plots alone when the caller's condition
does not hold, and write a plain-words note for every real change. The last test holds
the whole app to the rule that only plot_master/plot_status.py writes Plot Master.status.

Safety: every write is rolled back; a database commit or an outside call fails the test.
Uses one existing plot, whose status is set inside the test and rolled back.
"""

import os
import re
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms.landms.doctype.plot_master import plot_status
from landms.landms.doctype.plot_master.plot_status import ALLOWED_INTO, STATUSES, link, set_plot_status

# The 14 moves the app made before PM-1: (from, to) as each caller allowed them.
TODAYS_MOVES = [
	(
		"Available",
		"Pending Advance",
	),  # application submitted with no fee; fee recorded; order submitted/reopened
	("Pending Fee", "Pending Advance"),  # application fee recorded
	("Available", "Pending Fee"),  # application submitted
	("Pending Fee", "Available"),  # application cancelled / expired; order cancelled or closed
	("Pending Advance", "Available"),
	("Reserved", "Available"),  # application cancelled (unchecked move, kept)
	("Pending Advance", "Reserved"),  # contract submitted; advance paid
	*[(s, "Reserved") for s in STATUSES],  # contract submitted (unchecked, kept)
	*[(s, "Available") for s in STATUSES],  # contract cancelled / terminated (unchecked, kept)
	*[(s, "Ready for Handover") for s in STATUSES],  # handover cancelled (unchecked); contract fully paid
	*[(s, "Delivered") for s in STATUSES],  # handover submitted
]


def _no_commit():
	raise AssertionError(
		"a database commit happened inside the plot status test; nothing may be saved for real"
	)


def _no_outside_call(*args, **kwargs):
	raise AssertionError("an outside call was attempted inside the plot status test")


class TestPlotStatus(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=_no_outside_call)
		blocker.start()
		self.addCleanup(blocker.stop)
		self.plot = frappe.db.get_value("Plot Master", {}, "name")
		if not self.plot:
			self.skipTest("this site has no plot")

	def _put(self, status):
		frappe.db.set_value("Plot Master", self.plot, "status", status, update_modified=False)

	def _status(self):
		return frappe.db.get_value("Plot Master", self.plot, "status")

	def _notes(self):
		return frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Plot Master", "reference_name": self.plot, "comment_type": "Info"},
			pluck="content",
			order_by="creation desc",
		)

	def test_every_move_the_app_made_before_is_still_allowed(self):
		for from_status, to_status in TODAYS_MOVES:
			with self.subTest(move=f"{from_status} -> {to_status}"):
				self._put(from_status)
				self.assertTrue(set_plot_status(self.plot, to_status, "the test moved it"))
				self.assertEqual(self._status(), to_status)

	def test_a_move_no_code_makes_is_refused_and_nothing_changes(self):
		for from_status, to_status in (
			("Delivered", "Pending Fee"),
			("Reserved", "Pending Advance"),
			("Available", "Title Closed"),
		):
			with self.subTest(move=f"{from_status} -> {to_status}"):
				self._put(from_status)
				with self.assertRaises(frappe.ValidationError):
					set_plot_status(self.plot, to_status, "the test tried it")
				self.assertEqual(self._status(), from_status)
		with self.assertRaises(frappe.ValidationError):
			set_plot_status(self.plot, "Sold", "the test tried a status that does not exist")

	def test_the_callers_condition_still_decides(self):
		self._put("Reserved")
		before = len(self._notes())
		self.assertFalse(set_plot_status(self.plot, "Pending Fee", "x", only_from=("Available",)))
		self.assertEqual(self._status(), "Reserved")
		self.assertEqual(len(self._notes()), before)

	def test_a_change_leaves_a_plain_words_note_on_the_plot(self):
		self._put("Available")
		because = f"application {link('Plot Application', 'PA-TEST-0001')} was submitted"
		set_plot_status(self.plot, "Pending Fee", because)
		note = self._notes()[0]
		self.assertIn("Status: Available → Pending Fee", note)
		self.assertIn("by Administrator", note)
		self.assertIn("application", note)
		self.assertIn("PA-TEST-0001", note)

	def test_the_note_names_the_user_who_made_the_change(self):
		self._put("Available")
		user = frappe.db.get_value(
			"User", {"enabled": 1, "name": ["not in", ["Administrator", "Guest"]]}, "name"
		)
		if not user:
			self.skipTest("this site has no other user")
		self.addCleanup(frappe.set_user, frappe.session.user)
		frappe.set_user(user)
		set_plot_status(self.plot, "Pending Fee", "the test moved it")
		self.assertIn(f"by {frappe.utils.get_fullname(user)}", self._notes()[0])

	def test_resaving_the_same_status_writes_no_note(self):
		self._put("Reserved")
		before = len(self._notes())
		self.assertTrue(set_plot_status(self.plot, "Reserved", "the test saved it again"))
		self.assertEqual(len(self._notes()), before)

	def _listen(self):
		"""Stand in for another app that follows plots; returns what it was told."""
		told = []
		listener = patch.object(
			plot_status, "_listeners", return_value=[lambda **change: told.append(change)]
		)
		listener.start()
		self.addCleanup(listener.stop)
		return told

	def test_a_status_change_tells_the_apps_that_follow_plots(self):
		self._put("Available")
		told = self._listen()
		set_plot_status(self.plot, "Pending Fee", "the test submitted an application")
		self.assertEqual(told, [{"plot": self.plot, "from_status": "Available", "to_status": "Pending Fee"}])

	def test_nothing_is_told_when_the_status_does_not_change(self):
		self._put("Reserved")
		told = self._listen()
		set_plot_status(self.plot, "Reserved", "the test saved it again")
		set_plot_status(
			self.plot, "Pending Fee", "the caller wanted Available only", only_from=("Available",)
		)
		with self.assertRaises(frappe.ValidationError):
			set_plot_status(self.plot, "Pending Fee", "a move the rulebook refuses")
		self.assertEqual(told, [])

	def test_the_followers_are_the_functions_listed_under_the_hook(self):
		with patch.object(frappe, "get_hooks", return_value=["frappe.utils.cint"]) as hooks:
			self.assertEqual(plot_status._listeners(), [frappe.utils.cint])
		hooks.assert_called_once_with("plot_status_changed")
		# LandMS itself follows nothing: the hook is for other apps
		self.assertEqual(frappe.get_hooks("plot_status_changed", app_name="landms"), [])

	def test_a_plot_that_does_not_exist_is_ignored_quietly(self):
		self.assertFalse(set_plot_status("NO-SUCH-PLOT", "Available", "the test"))
		self.assertFalse(set_plot_status(None, "Available", "the test"))

	def test_every_status_is_in_the_rulebook(self):
		self.assertEqual(set(ALLOWED_INTO), set(STATUSES))

	def test_only_the_one_door_writes_a_plots_status(self):
		app = os.path.dirname(frappe.get_app_path("landms"))
		direct = re.compile(r"""set_value\(\s*["']Plot Master["'][^)]*["']status["']""", re.S)
		offenders = []
		for root, _dirs, files in os.walk(os.path.join(app, "landms")):
			if "tests" in root.split(os.sep):
				continue
			for name in files:
				path = os.path.join(root, name)
				if name.endswith(".py") and not path.endswith(os.path.join("plot_master", "plot_status.py")):
					if direct.search(open(path, encoding="utf-8").read()):
						offenders.append(os.path.relpath(path, app))
		self.assertEqual(offenders, [], "these files write Plot Master.status directly; use set_plot_status")
