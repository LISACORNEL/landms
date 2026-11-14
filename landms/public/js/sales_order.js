frappe.ui.form.on("Sales Order", {
  refresh(frm) {
    _render_so_payment_countdown(frm);
    _render_so_close_button(frm);
    _hide_erpnext_reopen(frm);
    _render_so_reopen_button(frm);
    _render_so_close_for_good_button(frm);
    _render_so_registration_banner(frm);
    _render_so_malipopay_banner(frm);
    _render_so_forfeiture_journals_button(frm);
  },

  setup(frm) {
    frm.set_query("plot_application", () => ({
      filters: {
        docstatus: 1,
        status: "Paid",
      },
    }));
  },

  plot_application(frm) {
    if (!frm.doc.plot_application) {
      frm.set_value("plot", "");
      frm.set_value("customer", "");
      frm.set_value("land_acquisition", "");
      frm.set_value("acquisition_name", "");
      frm.set_value("booking_fee_percent", 0);
      frm.set_value("government_share_percent", 0);
      frm.set_value("payment_completion_days", 0);
      frm.set_value("payment_deadline", "");
      frm.doc.items = [];
      frm.doc.payment_schedule = [];
      frm.refresh_fields();
      return;
    }
    frappe.call({
      method: "landms.sales_order_hooks.get_sales_order_defaults",
      args: { plot_application: frm.doc.plot_application },
      freeze: true,
      freeze_message: __("Loading plot details..."),
      callback(r) {
        if (!r.message) return;
        let d = r.message;
        frm.set_value("customer", d.customer);
        frm.set_value("plot", d.plot);
        frm.set_value("land_acquisition", d.land_acquisition);
        frm.set_value("acquisition_name", d.acquisition_name);
        frm.set_value("booking_fee_percent", d.booking_fee_percent);
        frm.set_value("government_share_percent", d.government_share_percent);
        frm.set_value("payment_completion_days", d.payment_completion_days);
        frm.set_value("transaction_date", d.transaction_date);
        frm.set_value("delivery_date", d.delivery_date);
        frm.set_value("payment_deadline", d.payment_deadline);
        if (d.cost_center && !frm.doc.cost_center) {
          frm.set_value("cost_center", d.cost_center);
        }
        frm.set_value("company", d.company);
        frm.set_value("set_warehouse", d.set_warehouse);

        frm.doc.items = [];
        let row = frm.add_child("items");
        Object.assign(row, d.item_row);

        frm.doc.payment_schedule = [];
        (d.schedule_rows || []).forEach((s) => {
          let ps = frm.add_child("payment_schedule");
          Object.assign(ps, s);
        });

        frm.refresh_fields();
      },
    });
  },
});

// ERPNext's Status > Re-open only flips the label of a closed plot order: the
// plot, contract and payment number stay closed (SO-05026, Oct 2026). Plot
// orders reopen through LandMS's own Reopen button only.
function _hide_erpnext_reopen(frm) {
  if (!frm.doc.plot || frm.doc.docstatus !== 1 || frm.doc.status !== "Closed")
    return;
  setTimeout(() => frm.remove_custom_button(__("Re-open"), __("Status")), 10);
}

function _render_so_close_button(frm) {
  if (frm.is_new()) return;
  if (!frm.doc.plot) return;
  if (frm.doc.docstatus !== 1) return;
  if (["Closed", "Cancelled"].includes(frm.doc.status)) return;

  setTimeout(() => {
    frm.remove_custom_button(__("Close"), __("Status"));
    frm.remove_custom_button(__("Hold"), __("Status"));
  }, 10);

  if (!frappe.user.has_role("LandMS Sales Manager")) return;

  frm.add_custom_button(
    __("Close Sales Order"),
    () => {
      frappe.confirm(
        __(
          "Close this Sales Order? The plot will be released and the payment number declined at TCB. If it has a Malipopay bill, the bill is paused when money was paid on it (Reopen brings it back) and ended when nothing was."
        ),
        () => {
          frappe.call({
            method: "landms.sales_order_hooks.close_sales_order",
            args: { sales_order_name: frm.doc.name },
            freeze: true,
            freeze_message: __("Closing Sales Order..."),
            callback(r) {
              if (r.exc) return;
              frm.reload_doc();
            },
          });
        }
      );
    },
    __("Actions")
  );
}

function _render_so_reopen_button(frm) {
  if (frm.is_new()) return;
  if (!frm.doc.plot) return;
  if (frm.doc.docstatus !== 1) return;
  // Only a Closed, forfeited order can be reopened.
  if (frm.doc.status !== "Closed") return;
  if (!frm.doc.forfeiture_entry) return;
  // Reopen (reversing a forfeiture) is System Manager only — more restricted than Close.
  if (!frappe.user.has_role("System Manager")) return;
  // Closed for good at Malipopay is final — the server refuses a reopen too.
  if (frm.doc.malipopay_closed_for_good) return;

  frm.add_custom_button(
    __("Reopen Sales Order"),
    () => {
      frappe.confirm(
        __(
          "Reopen this forfeited Sales Order? The forfeiture is reversed, the plot is reserved again, and a fresh 7-day advance window is set. The payment number is then re-registered with the bank. The government share is kept."
        ),
        () => {
          frappe.call({
            method: "landms.sales_order_hooks.reopen_sales_order",
            args: { sales_order_name: frm.doc.name },
            freeze: true,
            freeze_message: __("Reopening Sales Order..."),
            callback(r) {
              if (r.exc) return;
              frm.reload_doc();
            },
          });
        }
      );
    },
    __("Actions")
  );
}

function _render_so_close_for_good_button(frm) {
  if (frm.is_new()) return;
  if (!frm.doc.plot) return;
  if (frm.doc.docstatus !== 1) return;
  // The other way out of a Closed order: end its paused Malipopay bill for good.
  if (frm.doc.status !== "Closed") return;
  if (!frm.doc.forfeiture_entry) return; // closed with money — the orders Reopen also serves
  if (!frm.doc.malipopay_reference || frm.doc.malipopay_closed_for_good)
    return;
  // Same audience as Reopen: System Manager only (the server checks again).
  if (!frappe.user.has_role("System Manager")) return;

  frm.add_custom_button(
    __("Close for good at Malipopay"),
    () => {
      frappe.confirm(
        __(
          "Close this order for good at Malipopay? Its paused Malipopay bill {0} is expired — that is final — and this Sales Order can no longer be reopened. If the customer comes back, they need a new order.",
          [frm.doc.malipopay_reference]
        ),
        () => {
          frappe.call({
            method: "landms.malipopay.close_for_good_at_malipopay",
            args: { sales_order_name: frm.doc.name },
            freeze: true,
            freeze_message: __("Closing for good at Malipopay..."),
            callback(r) {
              if (r.exc) return;
              if (r.message && r.message.message) {
                frappe.show_alert({
                  message: r.message.message,
                  indicator: "green",
                });
              }
              frm.reload_doc();
            },
          });
        }
      );
    },
    __("Actions")
  );
}

function _render_so_payment_countdown(frm) {
  if (frm.is_new()) return;
  if (!frm.doc.payment_deadline) return;
  if (!frm.doc.plot) return;
  // Submitted orders only. The countdown and the registration banner both write to
  // the form's single headline/message element, so they must never render on the
  // same doc. The banner is Draft-only (docstatus 0); the countdown is submitted-only
  // (docstatus 1). This keeps them from colliding (one wiping or stacking on the other).
  if (frm.doc.docstatus !== 1) return;
  if (["Closed", "Completed"].includes(frm.doc.status)) return;

  frm.dashboard.clear_headline();

  const today = frappe.datetime.get_today();
  const deadline = frm.doc.payment_deadline;
  const days_remaining = frappe.datetime.get_day_diff(deadline, today);

  let indicator, message;
  if (days_remaining < 0) {
    indicator = "red";
    message = `🔴 Payment overdue by <strong>${Math.abs(
      days_remaining
    )} days</strong> — deadline was ${frappe.datetime.str_to_user(deadline)}`;
  } else if (days_remaining <= 30) {
    indicator = "orange";
    message = `🟡 <strong>${days_remaining} days remaining</strong> to complete payment — deadline: ${frappe.datetime.str_to_user(
      deadline
    )}`;
  } else {
    indicator = "green";
    message = `🟢 <strong>${days_remaining} days remaining</strong> to complete payment — deadline: ${frappe.datetime.str_to_user(
      deadline
    )}`;
  }

  frm.dashboard.set_headline_alert(message, indicator);
}

function _render_so_registration_banner(frm) {
  // Persistent notice for a plot Sales Order whose payment control number is not
  // yet live at the bank. Shows on a Draft order whose submit was blocked because
  // registration failed (the register-first gate leaves it in Draft). Clears on
  // its own once the number is registered. Uses set_intro so it does not collide
  // with the payment countdown, which owns the dashboard headline.
  if (frm.is_new()) return;
  // Plot Sales Orders only — mirrors _is_landms_sales_order on the server.
  if (!frm.doc.plot && !frm.doc.plot_application) return;
  // Only a Draft: a blocked submit leaves the order in Draft. A submitted order
  // always has a registered number (the gate would not have let it through).
  if (frm.doc.docstatus !== 0) return;
  // No control number yet means a fresh draft that was never submitted — nothing to warn about.
  if (!frm.doc.control_number) return;

  frappe.db
    .get_value("TCB Control Number", frm.doc.control_number, "status")
    .then((r) => {
      const status = r && r.message && r.message.status;
      if (["Failed", "Generated"].includes(status)) {
        frm.set_intro(
          "⚠ This order cannot receive payment yet. Its payment control number " +
            "has not been set up with the bank. Please try submitting again after a while.",
          "orange"
        );
      } else {
        frm.set_intro("");
      }
    });
}

const MP_BANNER_POLL_MS = 4000; // registration usually takes ~15 s
const MP_BANNER_MAX_POLLS = 30; // stop asking after 2 minutes

function _render_so_malipopay_banner(frm, poll) {
  // Submitted plot orders on a project that collects through Malipopay: say so
  // when the Malipopay bill is not working, with a link to it, and when TCB has
  // not set the number up yet (link to Waiting for TCB). The TCB banner above is
  // Draft-only, so the two never share the intro. Silent when all is well.
  // While the bill is still registering (it is created by a worker after the
  // save) it says so, asks again every few seconds, and clears itself once the
  // bill is Active. One server call that never throws; any failure shows nothing.
  poll = poll || 0;
  clearTimeout(frm._mp_banner_timer);
  if (frm.is_new() || frm.doc.docstatus !== 1) return;
  if (!frm.doc.plot || !frm.doc.control_number || !frm.doc.land_acquisition)
    return;
  if (["Closed", "Completed"].includes(frm.doc.status)) return;

  const order = frm.doc.name;
  frappe.call({
    method: "landms.malipopay.sales_order_malipopay_notice",
    args: { sales_order: order },
    error: () => {},
    callback(r) {
      if (frm.doc.name !== order) return; // the user has moved to another order
      const notice = (r && r.message) || {};
      if (!notice.message) {
        // clear only a message this banner put there itself
        if (frm._mp_banner_for === order) frm.set_intro("");
        frm._mp_banner_for = null;
        return;
      }
      let link = notice.collection
        ? ` <a href="/app/malipopay-collection/${encodeURIComponent(
            notice.collection
          )}">${frappe.utils.escape_html(notice.collection)}</a>`
        : "";
      if (notice.waiting_for_tcb) {
        link += ` <a href="/app/waiting-for-tcb/${encodeURIComponent(
          notice.waiting_for_tcb
        )}">${__("Waiting for TCB")}</a>`;
      }
      const mark = notice.pending ? "⏳ " : "⚠ ";
      frm.set_intro(
        mark + frappe.utils.escape_html(notice.message) + link,
        notice.indicator || "orange"
      );
      frm._mp_banner_for = order;
      if (notice.pending && poll < MP_BANNER_MAX_POLLS) {
        frm._mp_banner_timer = setTimeout(
          () => _render_so_malipopay_banner(frm, poll + 1),
          MP_BANNER_POLL_MS
        );
      }
    },
  });
}

// An order's forfeiture journals point back at it; this lists them, cancelled ones included.
function _render_so_forfeiture_journals_button(frm) {
  if (!frm.doc.plot || frm.is_new()) return;
  frm.add_custom_button(
    __("Forfeiture Journals"),
    () =>
      frappe.set_route("List", "Journal Entry", {
        lms_sales_order: frm.doc.name,
      }),
    __("View")
  );
}
