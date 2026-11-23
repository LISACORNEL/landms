frappe.query_reports["LandMS Bank Reference Search"] = {
  filters: [
    {
      fieldname: "reference",
      label: __("Bank Reference"),
      fieldtype: "Data",
      reqd: 1,
      description: __("A transaction id, payment number or part of one."),
    },
  ],
};
