// One settings record per project (LA-7): each account, warehouse and cost centre offers only
// the project's company.
frappe.ui.form.on("LandMS Project Setting", {
  setup(frm) {
    frm.meta.fields
      .filter((df) =>
        ["Account", "Warehouse", "Cost Center"].includes(df.options)
      )
      .forEach((df) =>
        frm.set_query(df.fieldname, () => ({
          filters: { company: frm.doc.company, is_group: 0 },
        }))
      );
  },
});
