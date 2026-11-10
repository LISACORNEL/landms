// Payment chain health (PB-6): zero rows means every payment's chain is complete.
frappe.query_reports["LandMS Payment Chain Health"] = {
  filters: [
    {
      fieldname: "land_acquisition",
      label: __("Project"),
      fieldtype: "Link",
      options: "Land Acquisition",
    },
  ],
};
