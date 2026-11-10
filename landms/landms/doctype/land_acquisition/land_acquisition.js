frappe.ui.form.on("Land Acquisition", {
  payment_years(frm) {
    _update_period_summary(frm);
  },
  payment_months(frm) {
    _update_period_summary(frm);
  },
  payment_days_input(frm) {
    _update_period_summary(frm);
  },

  refresh(frm) {
    _update_period_summary(frm);
    if (frm.doc.__islocal || !frm.doc.name) {
      // New doc — wipe any stale supplier tables left over from a
      // previously viewed Land Acquisition so the user doesn't see
      // somebody else's PO/PI/PE rows on their fresh form.
      ["land_seller_summary_html", "other_supplier_summary_html"].forEach(
        (f) => {
          const wrapper = frm.get_field(f)?.$wrapper;
          if (wrapper) wrapper.empty();
        }
      );
      return;
    }

    // Cancelled — show Delete button only
    if (frm.doc.docstatus === 2) {
      frm.clear_custom_buttons();
      if (frappe.model.can_delete("Land Acquisition")) {
        frm
          .add_custom_button(__("Delete"), function () {
            frappe.confirm(
              __("Permanently delete this Land Acquisition?"),
              function () {
                frappe.call({
                  method: "frappe.client.delete",
                  args: { doctype: "Land Acquisition", name: frm.doc.name },
                  callback: function () {
                    frappe.set_route("List", "Land Acquisition");
                  },
                });
              }
            );
          })
          .addClass("btn-danger");
      }
      return;
    }

    // Workflow status alerts
    if (frm.doc.docstatus === 0 && frm.doc.status === "Pending Approval") {
      frm.dashboard.set_headline_alert("Waiting for approval", "orange");
    } else if (frm.doc.docstatus === 1 && frm.doc.status === "Approved") {
      frm.dashboard.set_headline_alert("Approved", "green");
    } else if (frm.doc.docstatus === 1 && frm.doc.status === "Subdivided") {
      frm.dashboard.set_headline_alert("Approved and subdivided", "blue");
    }

    refresh_cost_summary(frm);
    refresh_plot_counts(frm);

    frm.add_custom_button(
      "Purchase Order",
      () => {
        frappe.flags.new_po_land_acquisition = frm.doc.name;
        frappe.new_doc("Purchase Order");
      },
      __("Create")
    );

    frm.add_custom_button(
      "Purchase Invoice",
      () => {
        frappe.flags.new_pi_land_acquisition = frm.doc.name;
        frappe.new_doc("Purchase Invoice");
      },
      __("Create")
    );

    if (frm.doc.recalculation_in_progress) {
      frm.dashboard.set_headline_alert(
        __(
          "Plot cost recalculation running in background — form will refresh when done."
        ),
        "blue"
      );
      _start_recalculation_poll(frm);
    } else if (frappe.user.has_role("System Manager")) {
      // The server refuses everyone else; no point offering them the button.
      frm.add_custom_button(__("Recalculate Plot Costs"), () => {
        frappe.confirm(
          __(
            "This will update stock valuations for all un-delivered plots to match the current acquisition cost.<br><br>" +
              "Delivered plots will be <b>skipped</b> — check the Recalculation Log for those.<br><br>" +
              "Runs in the background. Continue?"
          ),
          () => {
            frappe.call({
              method:
                "landms.landms.doctype.land_acquisition.recalculate_plot_costs.recalculate_plot_costs",
              args: { land_acquisition: frm.doc.name },
              callback: () => frm.reload_doc(),
            });
          }
        );
      });
    }
  },
});

function _start_recalculation_poll(frm) {
  const la_name = frm.doc.name;
  const poll = setInterval(() => {
    frappe.db.get_value(
      "Land Acquisition",
      la_name,
      "recalculation_in_progress",
      (r) => {
        if (!r || !r.recalculation_in_progress) {
          clearInterval(poll);
          frm.reload_doc();
          frappe.show_alert({
            message: __(
              "Plot cost recalculation complete. See Recalculation Log."
            ),
            indicator: "green",
          });
        }
      }
    );
  }, 3000);
}

function refresh_cost_summary(frm) {
  frappe.call({
    method:
      "landms.landms.doctype.land_acquisition.cost_totals.refresh_cost_summary",
    args: { land_acquisition: frm.doc.name },
    callback(r) {
      const summary = r.message || {};
      const totals = summary.totals || {};

      const fields = {
        acquisition_cost_tzs: totals.acquisition_cost_tzs,
        cost_per_sqm_tzs: totals.cost_per_sqm_tzs,
        total_committed_tzs: totals.committed,
        total_paid_tzs: totals.paid,
        total_outstanding_tzs: totals.outstanding,
        total_unbilled_po_tzs: totals.unbilled_po,
        je_billed_tzs: totals.je_billed,
      };
      for (const [name, value] of Object.entries(fields)) {
        frm.doc[name] = Number(value || 0);
        frm.refresh_field(name);
      }

      if (flt(frm.doc.total_area_sqm) > 0) {
        frm.set_df_property(
          "cost_per_sqm_tzs",
          "description",
          `${flt(totals.acquisition_cost_tzs).toLocaleString()} TZS ÷ ${flt(
            frm.doc.total_area_sqm
          ).toLocaleString()} sqm`
        );
      }

      render_je_table(
        frm,
        "je_summary_html",
        summary.je_rows || [],
        "No Journal Entries tagged to this Land Acquisition yet."
      );
      render_supplier_table(
        frm,
        "land_seller_summary_html",
        summary.sellers || [],
        "No land sellers tagged to this Land Acquisition yet."
      );
      render_supplier_table(
        frm,
        "other_supplier_summary_html",
        summary.others || [],
        "No other suppliers tagged to this Land Acquisition yet."
      );
    },
  });
}

function refresh_plot_counts(frm) {
  frappe.call({
    method:
      "landms.landms.doctype.land_acquisition.land_acquisition.refresh_plot_summary",
    args: { land_acquisition: frm.doc.name },
    callback(r) {
      const s = r.message || {};
      [
        "total_plots",
        "available_plots",
        "reserved_plots",
        "delivered_plots",
      ].forEach((f) => {
        frm.doc[f] = Number(s[f] || 0);
        frm.refresh_field(f);
      });
    },
  });
}

function render_je_table(frm, fieldname, rows, empty_message) {
  const wrapper = frm.get_field(fieldname)?.$wrapper;
  if (!wrapper) return;

  if (!rows.length) {
    wrapper.html(
      `<div class="text-muted" style="padding: 8px 0;">${empty_message}</div>`
    );
    return;
  }

  const escape_html = (v) => frappe.utils.escape_html(String(v || ""));
  const fmt = (v) => format_currency(v || 0, "TZS");
  const link = (name) =>
    `<a href="/app/journal-entry/${encodeURIComponent(
      name
    )}" target="_blank">${escape_html(name)}</a>`;

  const body = rows
    .map(
      (row) => `
        <tr>
            <td style="padding: 10px; white-space: nowrap;">${escape_html(
              row.posting_date || ""
            )}</td>
            <td style="padding: 10px;">${link(row.je_name)}</td>
            <td style="padding: 10px; font-size: 12px; color: #555;">${escape_html(
              row.user_remark || ""
            )}</td>
            <td class="text-right" style="padding: 10px; font-weight: 700; color: #1971c2;">${fmt(
              row.amount
            )}</td>
        </tr>
    `
    )
    .join("");

  const total = rows.reduce((sum, r) => sum + flt(r.amount), 0);

  wrapper.html(`
        <div class="table-responsive">
            <table class="table table-bordered" style="margin-bottom: 0; font-size: 13px;">
                <thead style="background-color: #f8f9fa;">
                    <tr>
                        <th style="padding: 10px; font-weight: 600;">Date</th>
                        <th style="padding: 10px; font-weight: 600;">Journal Entry</th>
                        <th style="padding: 10px; font-weight: 600;">Remarks</th>
                        <th class="text-right" style="padding: 10px; font-weight: 600;">Amount (TZS)</th>
                    </tr>
                </thead>
                <tbody>${body}</tbody>
                <tfoot style="background-color: #f8f9fa;">
                    <tr>
                        <td colspan="3" style="padding: 10px; font-weight: 700;">Total JE Billed</td>
                        <td class="text-right" style="padding: 10px; font-weight: 700; color: #1971c2;">${fmt(
                          total
                        )}</td>
                    </tr>
                </tfoot>
            </table>
        </div>
    `);
}

function render_supplier_table(frm, fieldname, rows, empty_message) {
  const wrapper = frm.get_field(fieldname)?.$wrapper;
  if (!wrapper) return;

  if (!rows.length) {
    wrapper.html(
      `<div class="text-muted" style="padding: 8px 0;">${empty_message}</div>`
    );
    return;
  }

  const escape_html = (v) => frappe.utils.escape_html(String(v || ""));
  const fmt = (v) => format_currency(v || 0, "TZS");

  const body = rows
    .map((row) => {
      const docs = build_drilldown_links(row);
      const outColor = row.outstanding > 0 ? "#d73a49" : "inherit";
      const unbColor = row.unbilled_po > 0 ? "#e36209" : "inherit";
      return `
            <tr>
                <td style="padding: 10px; vertical-align: middle;">
                    <strong>${escape_html(row.supplier_name)}</strong>
                    <div class="text-muted small">${escape_html(
                      row.supplier
                    )}</div>
                </td>
                <td class="text-right" style="padding: 10px; vertical-align: middle;">${fmt(
                  row.committed
                )}</td>
                <td class="text-right" style="padding: 10px; vertical-align: middle;">${fmt(
                  row.billed
                )}</td>
                <td class="text-right" style="padding: 10px; vertical-align: middle;">${fmt(
                  row.paid
                )}</td>
                <td class="text-right" style="padding: 10px; vertical-align: middle; color: ${outColor};">${fmt(
        row.outstanding
      )}</td>
                <td class="text-right" style="padding: 10px; vertical-align: middle; color: ${unbColor};">${fmt(
        row.unbilled_po
      )}</td>
                <td style="padding: 10px; vertical-align: middle; font-size: 12px;">${docs}</td>
            </tr>
        `;
    })
    .join("");

  wrapper.html(`
        <div class="table-responsive">
            <table class="table table-bordered" style="margin-bottom: 0; font-size: 13px;">
                <thead style="background-color: #f8f9fa;">
                    <tr>
                        <th style="padding: 10px; font-weight: 600;">Supplier</th>
                        <th class="text-right" style="padding: 10px; font-weight: 600;">Committed</th>
                        <th class="text-right" style="padding: 10px; font-weight: 600;">Billed</th>
                        <th class="text-right" style="padding: 10px; font-weight: 600;">Paid</th>
                        <th class="text-right" style="padding: 10px; font-weight: 600;">Outstanding</th>
                        <th class="text-right" style="padding: 10px; font-weight: 600;">Unbilled PO</th>
                        <th style="padding: 10px; font-weight: 600;">Documents</th>
                    </tr>
                </thead>
                <tbody>${body}</tbody>
            </table>
        </div>
    `);
}

function build_drilldown_links(row) {
  const escape_html = (v) => frappe.utils.escape_html(String(v || ""));
  const link = (doctype, name) =>
    `<a href="/app/${doctype}/${encodeURIComponent(
      name
    )}" target="_blank">${escape_html(name)}</a>`;

  const parts = [];
  if (row.po_docs && row.po_docs.length) {
    parts.push(
      `PO: ${row.po_docs
        .map((d) => link("purchase-order", d.name))
        .join(", ")}`
    );
  }
  if (row.pi_docs && row.pi_docs.length) {
    parts.push(
      `PI: ${row.pi_docs
        .map((d) => link("purchase-invoice", d.name))
        .join(", ")}`
    );
  }
  if (row.pe_docs && row.pe_docs.length) {
    const pes = row.pe_docs.map((d) => {
      const tag =
        d.against === "po" ? ` <span class="text-muted">(advance)</span>` : "";
      return `${link("payment-entry", d.name)}${tag}`;
    });
    parts.push(`PE: ${pes.join(", ")}`);
  }
  return parts.join("<br>") || '<span class="text-muted">-</span>';
}

function _update_period_summary(frm) {
  const years = cint(frm.doc.payment_years || 0);
  const months = cint(frm.doc.payment_months || 0);
  const days = cint(frm.doc.payment_days_input || 0);
  const total = years * 365 + months * 30 + days;

  const parts = [];
  if (years) parts.push(`${years} year${years > 1 ? "s" : ""}`);
  if (months) parts.push(`${months} month${months > 1 ? "s" : ""}`);
  if (days) parts.push(`${days} day${days > 1 ? "s" : ""}`);

  const label = parts.length ? parts.join(" + ") : "0 days";
  const summary = total > 0 ? `${label} = ${total} days total` : "";
  frm.set_value("payment_period_summary", summary);
}
