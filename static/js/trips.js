// trips browser behavior. Loaded as a classic script for CSP-safe cross-module bindings.

/**
 * Render the visible page of mileage claims and their payment state.
 * Stored names and statuses are escaped, and paid claims receive correction
 * controls instead of ordinary editing controls to preserve supporting evidence.
 */
function renderTrips() {
  const body = document.getElementById('trips-table-body');
  if (!body) return;
  const canManage = appRole !== Role.VOLUNTEER;
  body.innerHTML = state.trips.length
    ? state.trips.map((trip) => `
      <tr>
        <td>${dateLabel(trip.trip_date)}</td>
        <td>${personName(trip.person_id)}</td>
        <td>${escapeHtml(trip.origin_name || 'Legacy trip / origin not set')}</td>
        <td>${escapeHtml(trip.destination_name || 'Legacy trip / destination not set')}</td>
        <td>${escapeHtml(trip.tax_year || '\u2014')}</td>
        <td>${escapeHtml(trip.vehicle_type || '\u2014')}</td>
        <td>${Number(trip.total_miles || 0).toFixed(1)}</td>
        <td>${trip.declaration_accepted ? escapeHtml(trip.declaration_text) : 'Not accepted'}</td>
        <td>${trip.payment_id ? 'Paid' : escapeHtml(trip.status || '\u2014')}</td>
        ${canManage ? `<td>${trip.payment_id
          ? `<button class="btn btn-sm btn-outline-secondary" data-trip-corrections="${Number(trip.id)}">Corrections</button>`
          : `<button class="btn btn-sm btn-outline-danger" data-delete-trip="${Number(trip.id)}">Delete</button>`}</td>` : ''}
      </tr>`).join('')
    : `<tr><td colspan="${canManage ? 10 : 9}" class="text-center text-muted">No trips found</td></tr>`;
  applyTableSearch('trips-table-body');
}

/** Render declaration metadata without exposing unnecessary address details. */

async function loadTrips() {
  await loadTablePage('trips-table-body', '/trips', (data) => {
    state.trips = data.items || [];
    renderTrips();
  });
  if (document.getElementById('payment-trip-select')) await loadPaymentTripChoices();
}

/** Fetch declarations and update both the table and donation selectors. */

async function loadPaymentTripChoices() {
  const select = document.getElementById('payment-trip-select');
  if (!select) return;
  const personId = Number(document.getElementById('payment-person-select')?.value);
  const sequence = Number(select.dataset.loadSequence || 0) + 1;
  select.dataset.loadSequence = String(sequence);
  const trips = personId
    ? await loadAllChoices('/trips', { person_id: personId, unpaid: 'true' })
    : [];
  if (Number(select.dataset.loadSequence) !== sequence) return;
  state.tripChoices = trips;
  select.innerHTML = trips.length
    ? trips.map((trip) => `<option value="${trip.id}">${dateLabel(trip.trip_date)} \u00b7 ${escapeHtml(trip.destination_name || 'Unknown destination')} \u00b7 ${Number(trip.total_miles || 0).toFixed(1)} miles</option>`).join('')
    : '<option value="" disabled>No unpaid trips for this person</option>';
  updatePaymentAmount();
}

/**
 * Load all unpaid trip choices for the person selected on the payment form.
 * Choice loading is server-side and unpaginated by design so records beyond the
 * first table page remain available for reimbursement.
 */
function fillPaymentTripSelect() {
  loadPaymentTripChoices().catch((error) => alert(error.message));
}

/**
 * Request an authoritative reimbursement quote for all selected trips.
 * A second request sequence protects the amount from out-of-order responses.
 */

/**
 * Quote the selected trips and place the authoritative total into the payment form.
 * The server applies dated rates and annual mileage bands; this function only
 * presents the returned total and never reproduces financial rules in JavaScript.
 */
async function updatePaymentAmount() {
  const select = document.getElementById('payment-trip-select');
  const amount = document.getElementById('payment-amount');
  const help = document.getElementById('payment-trip-help');
  if (!select || !amount || !help) return;
  const requestSequence = ++paymentAmountRequestSequence;
  const tripIds = Array.from(select.selectedOptions, (option) => Number(option.value)).filter(Boolean);
  const submit = document.querySelector('#payment-form [type="submit"]');
  if (!tripIds.length) {
    amount.readOnly = false;
    amount.value = '';
    if (submit) submit.disabled = false;
    help.textContent = 'Select one or more unpaid trips for this person. A trip can only be linked to one payment.';
    return;
  }
  amount.readOnly = true;
  amount.value = '';
  if (submit) submit.disabled = true;
  help.textContent = 'Calculating the full reimbursement for selected trips\u2026';
  try {
    const quote = await api('/payments/quote', {
      method: 'POST',
      body: JSON.stringify({
        person_id: Number(document.getElementById('payment-person-select')?.value),
        trip_ids: tripIds,
      }),
    });
    if (requestSequence !== paymentAmountRequestSequence) return;
    const total = quote.total_amount_pence;
    amount.value = String(total);
    if (submit) submit.disabled = false;
    help.textContent = `Full reimbursement total: ${money(total)} for ${tripIds.length} selected trip${tripIds.length === 1 ? '' : 's'}. Car mileage bands accumulate per volunteer and tax year.`;
  } catch (error) {
    if (requestSequence !== paymentAmountRequestSequence) return;
    amount.value = '';
    help.textContent = error.message || 'Could not calculate the selected trips. Clear the selection or try again.';
  }
}

/** Display and record paid-trip corrections in a separate staff-only dialog. */
document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('trip-correction-form');
  if (!form) return;
  const list = document.getElementById('trip-corrections-list');
  const message = document.getElementById('trip-correction-message');
  const submit = form.querySelector('button[type="submit"]');
  const modal = bootstrap.Modal.getOrCreateInstance(form.closest('.modal'));
  let tripId;
  let loadSequence = 0;

  const displayCorrections = async () => {
    const sequence = ++loadSequence;
    const data = await api(`/trips/${tripId}/corrections`);
    if (sequence !== loadSequence) return;
    const entries = data.items.map((correction) => {
      const item = document.createElement('li');
      item.textContent = `${correction.created_at} UTC \u00b7 ${correction.note}`;
      return item;
    });
    if (entries.length === 0) {
      const item = document.createElement('li');
      item.textContent = 'No corrections have been recorded.';
      entries.push(item);
    }
    list.replaceChildren(...entries);
  };

  document.getElementById('trips-table-body')?.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-trip-corrections]');
    if (!button) return;
    tripId = Number(button.dataset.tripCorrections);
    form.reset();
    list.replaceChildren();
    message.textContent = '';
    modal.show();
    try {
      await displayCorrections();
    } catch (error) {
      message.textContent = error.message;
    }
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!tripId || submit.disabled) return;
    const savedTripId = tripId;
    submit.disabled = true;
    try {
      await api(`/trips/${savedTripId}/corrections`, {
        method: 'POST',
        body: JSON.stringify({ note: form.elements.note.value }),
      });
      if (tripId !== savedTripId) return;
      form.reset();
      message.textContent = 'Correction recorded.';
      await displayCorrections();
    } catch (error) {
      if (tripId === savedTripId) message.textContent = error.message;
    } finally {
      submit.disabled = false;
    }
  });
});


