// finance browser behavior. Loaded as a classic script for CSP-safe cross-module bindings.

/**
 * Render Gift Aid declarations from the current paginated application state.
 * The renderer exposes lifecycle status and edit controls while escaping every
 * person-supplied value before it is added to the document.
 */
function renderDeclarations() {
  const body = document.getElementById('declarations-table-body');
  if (!body) return;
  body.innerHTML = state.declarations.length
    ? state.declarations.map((entry) => `
      <tr>
        <td>${personName(entry.person_id)}</td>
        <td>${escapeHtml(entry.full_name)}</td>
        <td>${escapeHtml(entry.postcode || '\u2014')}</td>
        <td>${dateLabel(entry.declaration_date)}</td>
        <td>${escapeHtml(entry.scope || '\u2014')}</td>
        <td>${dateLabel(entry.withdrawn_date)}</td>
        <td><span class="status-badge ${entry.active ? '' : 'inactive'}">${entry.active ? 'Active' : 'Dormant'}</span></td>
        <td class="text-end text-nowrap">
          ${entry.has_evidence_image ? `<a class="btn btn-sm btn-outline-primary me-1" href="/declarations/${Number(entry.id)}/evidence-image" target="_blank" rel="noopener">View file</a>` : ''}
          <button class="btn btn-sm btn-light border me-1" data-edit-declaration="${entry.id}">Edit</button>
          <button class="btn btn-sm ${entry.active ? 'btn-outline-danger' : 'btn-outline-success'}" data-toggle-declaration="${entry.id}" data-active="${entry.active ? 'false' : 'true'}">${entry.active ? 'Make dormant' : 'Restore'}</button>
        </td>
      </tr>`).join('')
    : '<tr><td colspan="8" class="text-center text-muted">No declaration records</td></tr>';
  applyTableSearch('declarations-table-body');
}

/** Render donations with status, creator attribution, and edit/restore controls. */

function renderDonations() {
  const body = document.getElementById('donations-table-body');
  if (!body) return;
  body.innerHTML = state.donations.length
    ? state.donations.map((entry) => `
      <tr>
        <td>${personName(entry.person_id)}</td>
        <td>${money(entry.amount_pence)}</td>
        <td>${dateLabel(entry.donated_date)}</td>
        <td>${dateLabel(entry.received_date)}</td>
        <td>${escapeHtml(entry.method || '\u2014')}</td>
        <td>${entry.linked_payment_ids?.length
          ? entry.linked_payment_ids.map((paymentId) => `Payment #${Number(paymentId)}`).join(', ')
          : '\u2014'}</td>
        <td>${entry.gift_aid_eligible ? 'Yes' : 'No'}</td>
        <td><span class="status-badge ${entry.active ? '' : 'inactive'}">${entry.active ? 'Active' : 'Dormant'}</span></td>
        <td>${escapeHtml(entry.created_by || 'Unknown')}</td>
        <td>${escapeHtml(entry.created_at || '\u2014')}</td>
        <td class="text-end text-nowrap">
          <button class="btn btn-sm btn-light border me-1" data-edit-donation="${entry.id}">Edit</button>
          <button class="btn btn-sm ${entry.active ? 'btn-outline-danger' : 'btn-outline-success'}" data-toggle-donation="${entry.id}" data-active="${entry.active ? 'false' : 'true'}">${entry.active ? 'Make dormant' : 'Restore'}</button>
        </td>
      </tr>`).join('')
    : '<tr><td colspan="11" class="text-center text-muted">No donations recorded</td></tr>';
  applyTableSearch('donations-table-body');
}

/** Render reimbursements with covered trips and related active donations. */

function renderPayments() {
  const body = document.getElementById('payments-table-body');
  if (!body) return;
  body.innerHTML = state.payments.length
    ? state.payments.map((entry) => `
      <tr>
        <td>${personName(entry.person_id)}</td>
        <td>${money(entry.amount_pence)}</td>
        <td>${dateLabel(entry.paid_date)}</td>
        <td>${escapeHtml(entry.method || '\u2014')}</td>
        <td>${escapeHtml(entry.reference || '\u2014')}</td>
        <td>${entry.linked_trips?.length
          ? entry.linked_trips.map((trip) => `Trip #${trip.id} \u00b7 ${dateLabel(trip.trip_date)} \u00b7 ${escapeHtml(trip.destination_name || 'Unknown destination')}`).join('<br>')
          : '\u2014'}</td>
        <td>${money(entry.related_donation_amount_pence ?? entry.donated_back_pence)}${entry.linked_donation_count ? ` (${entry.linked_donation_count} related donation${entry.linked_donation_count === 1 ? '' : 's'}; reference only)` : ''}</td>
      </tr>`).join('')
    : '<tr><td colspan="7" class="text-center text-muted">No payments recorded</td></tr>';
  applyTableSearch('payments-table-body');
}

/** Render per-person totals and the difference between linked finances. */

function renderPaymentReconciliation() {
  const body = document.getElementById('reconciliation-table-body');
  if (!body) return;
  body.innerHTML = state.reconciliation.length
    ? state.reconciliation.map((entry) => `
      <tr>
        <td><span class="fw-semibold">${escapeHtml(entry.person_name)}</span></td>
        <td>${money(entry.payment_total_pence)}</td>
        <td>${money(entry.linked_donation_total_pence)}</td>
        <td class="fw-semibold">${money(entry.difference_pence)}</td>
        <td>${Number(entry.payment_count)} payment${entry.payment_count === 1 ? '' : 's'} \u00b7 ${Number(entry.linked_donation_count)} linked donation${entry.linked_donation_count === 1 ? '' : 's'}</td>
      </tr>`).join('')
    : '<tr><td colspan="5" class="empty-state">No payments have been recorded yet.</td></tr>';
  applyTableSearch('reconciliation-table-body');
}

/** Render account metadata returned by the admin-only users endpoint. */

async function loadDeclarations() {
  await loadTablePage('declarations-table-body', '/declarations', (data) => {
    state.declarations = data.items || [];
    renderDeclarations();
  });
  await Promise.all([
    loadDeclarationChoices('donation-person-select', 'donation-declaration-select'),
    loadDeclarationChoices('donation-edit-person-select', 'donation-edit-declaration-select'),
  ]);
}

/** Offer available declarations in both the add and edit donation forms. */

async function loadDeclarationChoices(personFieldId, selectId, selectedId = '') {
  const select = document.getElementById(selectId);
  if (!select) return;
  const personId = Number(document.getElementById(personFieldId)?.value);
  const sequence = Number(select.dataset.loadSequence || 0) + 1;
  select.dataset.loadSequence = String(sequence);
  const declarations = personId ? await loadAllChoices('/declarations', { person_id: personId }) : [];
  if (Number(select.dataset.loadSequence) !== sequence) return;
  state.declarationChoices = declarations;
  select.innerHTML = '<option value="">No declaration linked</option>' + declarations
    .filter((entry) => entry.active || String(entry.id) === String(selectedId))
    .map((entry) => `<option value="${entry.id}">${escapeHtml(entry.full_name)} \u00b7 ${escapeHtml(entry.scope || '')}</option>`)
    .join('');
  select.value = String(selectedId || '');
}

/**
 * Refresh the declaration selector for the person chosen on the donation form.
 * Loading choices from the server keeps inactive and unrelated declarations out
 * of the selectable set while preserving the form's current selection.
 */
function fillDeclarationSelect() {
  return Promise.all([
    loadDeclarationChoices('donation-person-select', 'donation-declaration-select'),
    loadDeclarationChoices('donation-edit-person-select', 'donation-edit-declaration-select'),
  ]);
}

/** Filter payment choices to match each donation form's selected person. */

async function loadPaymentChoices(personFieldId, selectId, selectedValues) {
  const select = document.getElementById(selectId);
  if (!select) return;
  const selectedIds = new Set((selectedValues || Array.from(select.selectedOptions, (option) => option.value)).map(String));
  const personId = Number(document.getElementById(personFieldId)?.value);
  const sequence = Number(select.dataset.loadSequence || 0) + 1;
  select.dataset.loadSequence = String(sequence);
  const payments = personId ? await loadAllChoices('/payments', { person_id: personId }) : [];
  if (Number(select.dataset.loadSequence) !== sequence) return;
  state.paymentChoices = payments;
  select.innerHTML = payments.map((payment) => {
    const label = `Payment #${payment.id} \u00b7 ${money(payment.amount_pence)} \u00b7 ${dateLabel(payment.paid_date)}`;
    return `<option value="${payment.id}" ${selectedIds.has(String(payment.id)) ? 'selected' : ''}>${escapeHtml(label)}</option>`;
  }).join('');
}

/**
 * Refresh payment choices for the donation form's currently selected person.
 * This wrapper centralizes error reporting for the asynchronous choice loader so
 * form initialization and later person changes behave consistently.
 */
function fillPaymentSelect() {
  return Promise.all([
    loadPaymentChoices('donation-person-select', 'donation-payment-select'),
    loadPaymentChoices('donation-edit-person-select', 'donation-edit-payment-select'),
  ]);
}

/** Offer only this person's trips that have not already been paid. */

async function loadDonations() {
  await loadTablePage('donations-table-body', '/donations', (data) => {
    state.donations = data.items || [];
    renderDonations();
  });
}

/** Fetch payments, then refresh donation choices and the payment table. */

async function loadPayments() {
  await loadTablePage('payments-table-body', '/payments', (data) => {
    state.payments = data.items || [];
    renderPayments();
  });
  await fillPaymentSelect();
}

/** Fetch the finance summary for the current Payments page. */

async function loadPaymentReconciliation() {
  const data = await api('/reports/payment-reconciliation');
  state.reconciliation = data.items || [];
  renderPaymentReconciliation();
}

/** Fetch and display club totals and per-volunteer report rows. */

async function editDonation(donationId) {
  const donation = state.donations.find((entry) => entry.id === donationId);
  const form = document.getElementById('donation-edit-form');
  if (!donation || !form) return;
  form.elements.namedItem('amount_pounds').value = (Number(donation.amount_pence) / 100).toFixed(2);
  Object.entries(donation).forEach(([key, value]) => {
    const field = form.elements.namedItem(key);
    if (!field) return;
    if (key === 'linked_payment_id' || key === 'linked_payment_ids') return;
    if (field.type === 'checkbox') field.checked = Boolean(Number(value));
    else field.value = value ?? '';
  });
  await Promise.all([
    loadPaymentChoices(
      'donation-edit-person-select',
      'donation-edit-payment-select',
      donation.linked_payment_ids || [],
    ),
    loadDeclarationChoices(
      'donation-edit-person-select',
      'donation-edit-declaration-select',
      donation.declaration_id,
    ),
  ]);
  bootstrap.Modal.getOrCreateInstance(document.getElementById('donation-edit-modal')).show();
}

/** Copy a declaration into the edit form, including dormant status. */

function editDeclaration(declarationId) {
  const declaration = state.declarations.find((entry) => entry.id === declarationId);
  const form = document.getElementById('declaration-edit-form');
  if (!declaration || !form) return;
  Object.entries(declaration).forEach(([key, value]) => {
    const field = form.elements.namedItem(key);
    if (!field) return;
    if (field.type === 'checkbox') field.checked = Boolean(Number(value));
    else field.value = value ?? '';
  });
  bootstrap.Modal.getOrCreateInstance(document.getElementById('declaration-edit-modal')).show();
}

/** Confirm a destructive action, call DELETE, and refresh after success. */


