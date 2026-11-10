const fieldHelpText = {
  'privacy-notice-file': 'Choose a UTF-8 .txt file (up to 64 KB) to replace the privacy statement shown to all users. Only administrators can upload it.',
  'trips-date-from': 'Show trips on or after this trip date. Clear the field to remove the start-date limit.',
  'trips-date-to': 'Show trips on or before this trip date. Clear the field to remove the end-date limit.',
  'donations-date-from': 'Show donations made on or after this donation date.',
  'donations-date-to': 'Show donations made on or before this donation date.',
  'payments-date-from': 'Show payments made on or after this paid date.',
  'payments-date-to': 'Show payments made on or before this paid date.',
  'destination-csv-file': 'Choose a UTF-8 CSV with a required name column to import several locations. The whole file is validated before locations are saved.',
  'trip-csv-file': 'Choose a UTF-8 CSV with the required trip columns shown in the upload dialog. Admins assign people; other users are restricted to their linked person.',
  'trip-person-select': 'Select which person owns this trip. Only administrators can choose another person.',
  'trip-origin-select': 'Select the active location where this trip begins. The club home ground is selected by default when configured.',
  'trip-destination-select': 'Select the active location where this trip ends. This selection is used with the origin to calculate road mileage.',
  'trip-date': 'The date the trip took place; it is used for the trip record and tax-year calculation.',
  'trip-vehicle': 'Choose the vehicle used. The vehicle type determines the applicable mileage reimbursement rate.',
  'trip-miles': 'The one-way road distance between the selected locations. This is calculated automatically and is not editable.',
  'trip-passengers': 'The number of eligible adult volunteers carried, excluding the driver. This may affect reimbursement.',
  'return-trip': 'Select if the journey included a return to the origin; the recorded distance is doubled.',
  'declaration-accepted': 'Confirm that the claimant has read and accepted the mileage declaration before submitting.',
  'payment-trip-select': 'Select one or more unpaid trips reimbursed by this payment. Each trip can be linked to only one payment.',
  trip_ids: 'Select one or more unpaid trips reimbursed by this payment. Each trip can be linked to only one payment.',
  'payment-amount': 'The payment total in pence. When trips are selected, the system calculates the full reimbursement amount.',
  'payment-person-select': 'Select the person receiving this reimbursement. The trip list is filtered to their unpaid trips.',
  'payment-date': 'The date the reimbursement was paid. Leave blank if the date is not recorded.',
  'user-role-select': 'Choose the access level. Admins manage the whole system; treasurers handle club records; volunteers access their own trip records.',
  'user-person-select': 'Link this account to a person record. Volunteers require a link; treasurers need one to enter trips for themselves.',
  'user-password': 'The initial sign-in password. Use at least 12 characters and share it securely with the account holder.',
  'person-active': 'Inactive people are retained for existing history but should not be selected for new activity.',
  'declaration-person-select': 'Select the person associated with this Gift Aid declaration.',
  'donation-edit-person-select': 'Select the person associated with this donation.',
  'donation-person-select': 'Select the person who made this donation.',
  'donation-edit-payment-select': 'Select one or more same-person reimbursement payments for reference. The donation amount is not allocated or offset.',
  'donation-payment-select': 'Select one or more same-person reimbursement payments for reference. The donation amount is not allocated or offset.',
  'donation-edit-amount': 'The donation amount in pence.',
  'donation-amount': 'The donation amount in pence.',
  'donation-edit-gift-aid-eligible': 'Mark eligible only after checking donor eligibility and a valid declaration for this donation.',
  'gift-aid-eligible': 'Mark eligible only after checking donor eligibility and a valid declaration for this donation.',
  'destination-home-ground': 'Set this location as the club’s home ground and default trip origin. This replaces any existing home-ground setting.',
  'edit-destination-home-ground': 'Set this active location as the club’s default trip origin, replacing any existing home ground.',
  name: 'The full name or team name used to identify this record.',
  email: 'An email address for contacting this person. Leave blank if not available.',
  phone: 'A telephone number for contacting this person. Leave blank if not available.',
  role: 'The type of contact or the access level granted to this account.',
  active: 'Inactive records are retained for history but are not available for new activity.',
  notes: 'Optional information to help staff understand or identify this record.',
  address: 'The street address of the person or ground.',
  home_address: 'The donor’s home address as recorded for their Gift Aid declaration.',
  town_city: 'The town or city where the ground is located.',
  postcode: 'The postcode for the address or location.',
  home_ground: 'Marks this active location as the club’s default trip origin. Only one location can be the home ground.',
  person_id: 'The person this record belongs to. Non-admin trip entry is restricted to the signed-in user’s linked person.',
  person_name: 'For admin CSV imports, the exact name of the person this trip belongs to. It must match one person record.',
  origin_id: 'The active location where the trip starts. CSV files use the location’s numeric ID.',
  destination_id: 'The active location where the trip ends. CSV files use the location’s numeric ID.',
  trip_date: 'The calendar date the trip took place.',
  vehicle_type: 'The vehicle used for the trip; this affects the mileage reimbursement rate.',
  miles_one_way: 'The road distance from origin to destination, excluding the return journey.',
  passengers: 'The number of eligible adult volunteers carried, excluding the driver.',
  return_trip: 'Select this when the journey includes a return to the origin; mileage is doubled.',
  declaration_accepted: 'Confirms that the claimant has read and accepted the displayed mileage declaration.',
  amount_pence: 'The amount in pence. For payments linked to trips, this must match their full reimbursement total.',
  donated_date: 'The date the donor made the contribution.',
  received_date: 'The date the club received the contribution.',
  paid_date: 'The date the reimbursement was paid.',
  method: 'How the donation or reimbursement was paid.',
  reference: 'An optional transaction or bookkeeping reference.',
  bank_reference: 'The reference shown on the bank transaction, if applicable.',
  declaration_id: 'The Gift Aid declaration that supports this donation. Select only a relevant declaration.',
  linked_payment_ids: 'Optional same-person reimbursement payments linked for reference only. The donation amount is not allocated or offset.',
  linked_payment_id: 'An optional reimbursement payment linked for reference only; neither amount is changed or offset.',
  gift_aid_eligible: 'Mark only after verifying donor eligibility and a valid declaration for this donation.',
  full_name: 'The donor’s full legal name as it appears on the Gift Aid declaration.',
  declaration_date: 'The date the donor made the Gift Aid declaration.',
  scope: 'Indicates whether the declaration covers future, this, or past and future donations.',
  evidence_ref: 'An optional reference to where the signed or recorded declaration evidence is stored.',
  withdrawn_date: 'The date the donor withdrew the declaration, if applicable.',
  username: 'The name the user enters to sign in.',
  password: 'The initial account password. It must be at least 12 characters.',
  file: 'Choose a CSV file from your device. The upload is checked before records are saved.',
};

/** Prefer exact field IDs, then shared field names, before label-based guidance. */
function fieldDescription(field, label) {
  if (field.id && fieldHelpText[field.id]) return fieldHelpText[field.id];
  if (field.name && fieldHelpText[field.name]) return fieldHelpText[field.name];
  if (field.matches('[data-table-search]')) {
    return 'Search the records currently displayed in this table. The search does not change or remove any records.';
  }
  const fieldLabel = label?.textContent?.trim() || field.getAttribute('aria-label') || 'this field';
  return `Enter or select ${fieldLabel.toLocaleLowerCase()} for this record.`;
}

/** Attach a keyboard-accessible popover that explains one visible form field. */
function addFieldHelp(field) {
  if (field.type === 'hidden' || field.type === 'submit' || field.type === 'button' || field.type === 'reset') return;
  const label = field.id
    ? Array.from(document.querySelectorAll('label[for]')).find((candidate) => candidate.htmlFor === field.id)
    : field.closest('label') || (field.previousElementSibling?.tagName === 'LABEL' ? field.previousElementSibling : null);
  const existing = label?.nextElementSibling;
  if (existing?.classList.contains('field-help-button')) return;

  const helpButton = document.createElement('button');
  helpButton.type = 'button';
  helpButton.className = 'field-help-button btn btn-sm btn-outline-secondary';
  helpButton.textContent = '?';
  helpButton.setAttribute('aria-label', `Help for ${label?.textContent?.trim() || field.getAttribute('aria-label') || 'field'}`);
  helpButton.setAttribute('data-bs-toggle', 'popover');
  helpButton.setAttribute('data-bs-trigger', 'focus');
  helpButton.setAttribute('data-bs-placement', 'auto');
  helpButton.setAttribute('data-bs-title', 'About this field');
  helpButton.setAttribute('data-bs-content', fieldDescription(field, label));

  if (label) {
    label.insertAdjacentElement('afterend', helpButton);
  } else {
    const wrapper = field.closest('.table-search-wrap') || field.parentElement;
    if (wrapper) wrapper.append(helpButton);
  }
  new bootstrap.Popover(helpButton);
}

document.addEventListener('DOMContentLoaded', () => {
  // Hidden state and action buttons are not data-entry fields and need no help popover.
  document.querySelectorAll('input, select, textarea').forEach(addFieldHelp);
});
