frappe.ui.form.on("Plot Application", {
  setup(frm) {
    // Only Available plots can be applied for
    frm.set_query("plot", () => ({
      filters: { status: "Available" },
    }));
  },

  refresh(frm) {
    const colors = {
      Draft: "gray",
      Submitted: "blue",
      Paid: "green",
      Converted: "purple",
      Expired: "red",
      Cancelled: "red",
    };
    frm.page.set_indicator(frm.doc.status, colors[frm.doc.status] || "gray");

    // Record Fee Payment — only on Submitted applications
    if (frm.doc.docstatus === 1 && frm.doc.status === "Submitted") {
      frm.add_custom_button(
        __("Record Fee Payment"),
        () => {
          open_fee_payment_dialog(frm);
        },
        __("Actions")
      );
    }

    // Jump to the linked Sales Order
    if (frm.doc.sales_order) {
      frm.add_custom_button(
        __("View Sales Order"),
        () => {
          frappe.set_route("Form", "Sales Order", frm.doc.sales_order);
        },
        __("Actions")
      );
    }
  },

  plot(frm) {
    if (!frm.doc.plot) {
      frm.set_value("land_acquisition", "");
      frm.set_value("acquisition_name", "");
      return;
    }
    frappe.db.get_doc("Plot Master", frm.doc.plot).then((plot_doc) => {
      if (plot_doc.status !== "Available") {
        frappe.msgprint({
          title: __("Plot Not Available"),
          message: __(
            'Plot {0} has status "{1}". Only Available plots can be applied for.',
            [frm.doc.plot, plot_doc.status]
          ),
          indicator: "red",
        });
        frm.set_value("plot", "");
        frm.set_value("land_acquisition", "");
        frm.set_value("acquisition_name", "");
        return;
      }
      frm.set_value("land_acquisition", plot_doc.land_acquisition);
      frappe.db
        .get_value(
          "Land Acquisition",
          plot_doc.land_acquisition,
          "acquisition_name"
        )
        .then((r) => {
          frm.set_value(
            "acquisition_name",
            (r.message && r.message.acquisition_name) || ""
          );
        });
    });
  },
});

function open_fee_payment_dialog(frm) {
  const render_dialog = (defaultBankAccount) => {
    const d = new frappe.ui.Dialog({
      title: __("Record Application Fee Payment"),
      fields: [
        {
          fieldname: "fee_info",
          fieldtype: "HTML",
          options: `<p style="margin-bottom:10px">Application Fee: <b>TZS ${(
            frm.doc.application_fee || 0
          ).toLocaleString()}</b></p>`,
        },
        {
          fieldname: "payment_date",
          fieldtype: "Date",
          label: __("Payment Date"),
          reqd: 1,
          default: frappe.datetime.get_today(),
        },
        {
          fieldname: "bank_account",
          fieldtype: "Link",
          label: __("Received In (Bank/Cash)"),
          options: "Account",
          default: defaultBankAccount || "",
          description: __(
            "Defaults from the project's settings (Application Fee Receiving Account)."
          ),
          get_query() {
            return {
              filters: {
                is_group: 0,
                account_type: ["in", ["Bank", "Cash"]],
              },
            };
          },
        },
        {
          fieldname: "reference_no",
          fieldtype: "Data",
          label: __("Reference No"),
          description: __("Bank receipt / transaction reference"),
        },
      ],
      primary_action_label: __("Record Payment"),
      primary_action(values) {
        frappe.call({
          method: "record_fee_payment",
          doc: frm.doc,
          args: {
            payment_date: values.payment_date,
            bank_account: values.bank_account || "",
            reference_no: values.reference_no || "",
          },
          freeze: true,
          freeze_message: __("Recording payment & creating Sales Order..."),
          callback(r) {
            if (!r.exc) {
              d.hide();
              frm.reload_doc();
            }
          },
        });
      },
    });
    d.show();
  };

  frappe.db
    .get_value(
      "LandMS Project Setting",
      frm.doc.land_acquisition,
      "application_fee_receiving_account"
    )
    .then((r) =>
      render_dialog(
        (r.message && r.message.application_fee_receiving_account) || ""
      )
    )
    .catch(() => render_dialog(""));
}
