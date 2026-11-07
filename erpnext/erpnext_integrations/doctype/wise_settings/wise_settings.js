// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

const WISE_METHOD = "erpnext.erpnext_integrations.doctype.wise_settings.wise_settings";

frappe.ui.form.on("Wise Settings", {
	refresh(frm) {
		if (!frm.doc.enabled || frm.is_dirty()) {
			return;
		}

		frm.add_custom_button(__("Select Profile"), () => select_profile(frm));

		if (!frm.doc.profile_id) {
			return;
		}

		frm.add_custom_button(__("Get Bank Accounts"), () => {
			frappe.call({
				method: `${WISE_METHOD}.add_bank_accounts`,
				freeze: true,
				freeze_message: __("Reading your Wise balances..."),
				callback: (r) => {
					frappe.msgprint({
						title: __("Bank Accounts"),
						message: (r.message || [])
							.map((name) => frappe.utils.get_form_link("Bank Account", name, true))
							.join("<br>"),
						indicator: "green",
					});
				},
			});
		});

		frm.add_custom_button(__("Sync Now"), () => {
			frappe.call({
				method: `${WISE_METHOD}.enqueue_synchronization`,
				freeze: true,
				callback: () => {
					frappe.show_alert({
						message: __(
							"The sync has started in the background, please check the {0} list for new records.",
							[frappe.utils.get_form_link("Bank Transaction", "", true, __("Bank Transaction"))]
						),
						indicator: "blue",
					});
				},
			});
		}).addClass("btn-primary");
	},
});

function select_profile(frm) {
	frappe.call({
		method: `${WISE_METHOD}.get_profiles`,
		freeze: true,
		callback: (r) => {
			const profiles = r.message || [];
			const dialog = new frappe.ui.Dialog({
				title: __("Select Profile"),
				fields: [
					{
						fieldname: "profile_id",
						fieldtype: "Select",
						label: __("Profile"),
						reqd: 1,
						options: profiles.map((profile) => ({
							value: profile.id,
							label: `${profile.name} (${__(frappe.utils.to_title_case(profile.type || ""))})`,
						})),
						default: frm.doc.profile_id,
					},
				],
				primary_action_label: __("Select"),
				primary_action({ profile_id }) {
					const profile = profiles.find((p) => p.id === profile_id);
					frm.set_value({ profile_id, profile_name: profile.name });
					frm.save();
					dialog.hide();
				},
			});
			dialog.show();
		},
	});
}
