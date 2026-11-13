// Keep browser-side display decisions aligned with the Python Role enum.  The
// server remains authoritative for every permission check.
const Role = Object.freeze({
  ADMIN: 'admin',
  TREASURER: 'treasurer',
  VOLUNTEER: 'volunteer',
});

// Shared client-side state: each page loads only the records needed by its view.
// Request counters prevent older async responses from overwriting newer choices.
const currencyFormatter = new Intl.NumberFormat('en-GB', {
  style: 'currency',
  currency: 'GBP',
  minimumFractionDigits: 2,
});

const state = { people: [], trips: [], destinations: [], declarations: [], donations: [], payments: [], reconciliation: [], users: [], audit: [] };
const appRole = document.body.dataset.role || '';
let routeRequestSequence = 0;
let paymentAmountRequestSequence = 0;

/**
 * Call a Flask JSON endpoint, attaching CSRF on writes and surfacing API errors.
 * The helper accepts non-JSON responses too, so unexpected server output is visible.
 */
async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (options.method && options.method.toUpperCase() !== 'GET') {
    headers['X-CSRF-Token'] = document.querySelector('meta[name="csrf-token"]').content;
  }
  const response = await fetch(path, { ...options, headers });
  const data = (response.headers.get('content-type') || '').includes('application/json')
    ? await response.json()
    : await response.text();
  if (!response.ok) throw new Error(data.error || 'Request failed');
  return data;
}

/** Draw the location list and reapply any active table search. */
function renderDestinations() {
  const body = document.getElementById('destinations-table-body');
  if (!body) return;
  const count = document.getElementById('destinations-count-label');
  if (count) count.textContent = `${state.destinations.length} location${state.destinations.length === 1 ? '' : 's'}`;
  body.innerHTML = state.destinations.length
    ? state.destinations.map((destination) => `
      <tr>
        <td><span class="fw-semibold">${escapeHtml(destination.name)}</span>${destination.home_ground ? '<span class="badge rounded-pill text-bg-success-subtle ms-2">Home</span>' : ''}</td>
        <td>${escapeHtml(destination.address || '—')}</td>
        <td>${escapeHtml(destination.town_city || '—')}</td>
        <td>${escapeHtml(destination.postcode || '—')}</td>
        <td>${destination.home_ground ? '<span class="text-success fw-semibold">Yes</span>' : '<span class="text-muted">—</span>'}</td>
        <td><span class="status-badge ${destination.active ? '' : 'inactive'}">${destination.active ? 'Active' : 'Inactive'}</span></td>
        <td class="text-end text-nowrap">
          <button class="btn btn-sm btn-light border me-1" data-edit-destination="${destination.id}" aria-label="Edit ${escapeHtml(destination.name)}">Edit</button>
          <button class="btn btn-sm btn-outline-danger" data-delete-destination="${destination.id}" aria-label="Delete ${escapeHtml(destination.name)}">Delete</button>
        </td>
      </tr>`).join('')
    : '<tr><td colspan="7" class="empty-state">No locations found. Add a location to get started.</td></tr>';
  applyTableSearch('destinations-table-body');
}

/** Populate trip origin/destination controls and choose the configured home ground. */
function fillDestinationSelect() {
  const active = state.destinations.filter((destination) => destination.active);
  const destinationSelect = document.getElementById('trip-destination-select');
  if (destinationSelect) {
    destinationSelect.innerHTML = '<option value="">Select a destination</option>' + active
      .map((destination) => `<option value="${destination.id}">${escapeHtml(destination.name)}</option>`)
      .join('');
  }
  const originSelect = document.getElementById('trip-origin-select');
  if (originSelect) {
    originSelect.innerHTML = '<option value="">Select an origin</option>' + active
      .map((destination) => `<option value="${destination.id}">${escapeHtml(destination.name)}</option>`)
      .join('');
    const homeGround = active.find((destination) => destination.home_ground);
    if (homeGround) originSelect.value = String(homeGround.id);
  }
}

/**
 * Request a fresh road route when both trip endpoints are selected.
 * Sequence IDs discard stale replies if the user changes locations mid-request.
 */
async function updateTripMileage() {
  const origin = document.getElementById('trip-origin-select');
  const destination = document.getElementById('trip-destination-select');
  const miles = document.getElementById('trip-miles');
  const status = document.getElementById('trip-route-status');
  if (!origin || !destination || !miles || !status) return;

  const requestSequence = ++routeRequestSequence;
  miles.value = '';
  if (!origin.value || !destination.value) {
    status.textContent = 'Select an origin and destination to calculate road mileage.';
    status.classList.remove('text-danger', 'text-success');
    status.classList.add('text-muted');
    return;
  }

  status.textContent = 'Calculating road mileage…';
  status.classList.remove('text-danger', 'text-success', 'text-muted');
  try {
    const result = await api(
      `/api/route-distance?origin_id=${encodeURIComponent(origin.value)}&destination_id=${encodeURIComponent(destination.value)}`,
    );
    if (requestSequence !== routeRequestSequence) return;
    miles.value = Number(result.distance_miles).toFixed(1);
    status.textContent = `Road mileage calculated using ${result.provider}.`;
    status.classList.add('text-success');
  } catch (error) {
    if (requestSequence !== routeRequestSequence) return;
    status.textContent = error.message || 'Unable to calculate mileage. Check the location addresses and try again.';
    status.classList.add('text-danger');
  }
}

/** Escape untrusted API text before inserting it into HTML strings. */
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[char]));
}

/** Fetch locations once and update both the masterfile and trip selectors. */
async function loadDestinations() {
  const data = await api('/destinations');
  state.destinations = data.items || [];
  fillDestinationSelect();
  renderDestinations();
}

/** Format integer pence as a localized pound amount. */
function money(value) {
  return currencyFormatter.format((Number(value) || 0) / 100);
}

/** Copy a location's stored values into the edit modal and display it. */
function editDestination(destinationId) {
  const destination = state.destinations.find((entry) => entry.id === destinationId);
  if (!destination) return;
  const form = document.getElementById('destination-edit-form');
  if (!form) return;
  Object.entries(destination).forEach(([key, value]) => {
    const field = form.elements.namedItem(key);
    if (!field) return;
    if (field.type === 'checkbox') field.checked = Boolean(Number(value));
    else if (key === 'active' && field.tagName === 'SELECT') field.value = Number(value) ? 'true' : 'false';
    else field.value = value ?? '';
  });
  bootstrap.Modal.getOrCreateInstance(document.getElementById('destination-edit-modal')).show();
}

/** Format an ISO date for display without timezone-shifting the calendar day. */
function dateLabel(value) {
  if (!value) return '—';
  const parsed = new Date(`${value}T00:00:00`);
  return Number.isNaN(parsed.getTime()) ? escapeHtml(value) : parsed.toLocaleDateString('en-GB');
}

/** Resolve a person ID against the in-memory people list for table labels. */
function personName(personId) {
  const person = state.people.find((entry) => entry.id === personId);
  return escapeHtml(person ? person.name : 'Unknown');
}

/** Put the dashboard counts and totals into their metric cards. */
function renderSummary(summary) {
  const values = {
    'people-count': summary.people,
    'trips-count': summary.trips,
    'donations-total': money(summary.donations_pence),
    'payments-total': money(summary.payments_pence),
  };
  Object.entries(values).forEach(([id, value]) => {
    const element = document.getElementById(id);
    if (element) element.textContent = value ?? 0;
  });
}

/** Render role-scoped dashboard reminders without exposing staff data to volunteers. */
function renderDashboardOverview(overview) {
  const actions = document.getElementById('dashboard-next-steps');
  if (actions) {
    if (appRole === Role.VOLUNTEER) {
      const recentTrips = overview.recent_trips || [];
      const recentList = recentTrips.length
        ? `<ul class="dashboard-recent-list mb-0">${recentTrips.map((trip) => `
            <li><span>${dateLabel(trip.trip_date)} · ${escapeHtml(trip.destination_name || 'Destination not recorded')}</span><span class="status-badge">${escapeHtml(trip.status || 'submitted')}</span></li>
          `).join('')}</ul>`
        : '<p class="text-muted mb-0">No trips have been submitted yet.</p>';
      actions.innerHTML = `
        <div class="col-12 col-md-8 col-xl-6">
          <article class="card dashboard-action-card h-100">
            <div class="card-body">
              <div class="metric-label">Your trip history</div>
              <div class="metric-value">${Number(overview.trip_count) || 0}</div>
              <div class="metric-note mb-3">Trips linked to your account</div>
              ${recentList}
              <a class="btn btn-outline-primary btn-sm mt-3" href="/trips/page">View my trips</a>
            </div>
          </article>
        </div>`;
      return;
    }

    // Staff reminders are informational links, not approvals or tax eligibility decisions.
    const reminders = [];
    if (overview.trips_without_payment > 0) {
      reminders.push({
        title: 'Trips without a recorded reimbursement',
        detail: `${overview.trips_without_payment} trip${overview.trips_without_payment === 1 ? ' has' : 's have'} no payment linked in this app.`,
        href: '/payments/page',
        link: 'Review payments',
      });
    }
    if (overview.eligible_donations_without_active_declaration > 0) {
      reminders.push({
        title: 'Check Gift Aid declaration links',
        detail: `${overview.eligible_donations_without_active_declaration} active donation${overview.eligible_donations_without_active_declaration === 1 ? '' : 's'} marked eligible have no active, unwithdrawn declaration linked.`,
        href: '/donations/page',
        link: 'Review donations',
      });
    }
    if (!overview.has_active_home_ground) {
      reminders.push({
        title: 'Set the club home ground',
        detail: 'No active location is marked as the default trip origin.',
        href: '/destinations/page',
        link: 'Review locations',
      });
    }
    actions.innerHTML = reminders.length
      ? reminders.map((reminder) => `
          <div class="col-12 col-md-6 col-xl-4">
            <article class="card dashboard-action-card h-100">
              <div class="card-body d-flex flex-column">
                <h3 class="h6">${escapeHtml(reminder.title)}</h3>
                <p class="text-muted small">${escapeHtml(reminder.detail)}</p>
                <a class="btn btn-outline-primary btn-sm mt-auto align-self-start" href="${reminder.href}">${escapeHtml(reminder.link)}</a>
              </div>
            </article>
          </div>`).join('')
      : '<div class="col-12"><div class="alert alert-success mb-0" role="status">No reminders from these dashboard checks. Continue recording activity and review the relevant records regularly.</div></div>';
  }

  const activity = document.getElementById('dashboard-recent-activity');
  if (!activity) return;
  const events = overview.recent_activity || [];
  activity.innerHTML = events.length
    ? events.map((event) => `
        <li class="dashboard-activity-item">
          <span><strong>${escapeHtml(event.actor_username || 'Unknown user')}</strong> ${escapeHtml(event.action)} ${escapeHtml(event.entity_type)} #${escapeHtml(event.entity_id)}</span>
          <time>${escapeHtml(event.occurred_at || '')}</time>
        </li>`).join('')
    : '<li class="text-muted">No audited activity has been recorded yet.</li>';
}

/** Render immutable, complete rate schedules with their derived end dates. */
function renderMileageRateSchedules(schedules) {
  const body = document.getElementById('mileage-rate-table-body');
  if (!body) return;
  const count = document.getElementById('mileage-rate-count');
  if (count) count.textContent = `${schedules.length} schedule${schedules.length === 1 ? '' : 's'}`;
  // The backend derives each end date from the next schedule without rewriting history.
  body.innerHTML = schedules.length
    ? schedules.map((schedule, index) => {
      const start = index === 0 && schedule.effective_from === '0001-01-01'
        ? 'Original app rates'
        : dateLabel(schedule.effective_from);
      const period = schedule.effective_to
        ? `${start} – ${dateLabel(schedule.effective_to)}`
        : `${start} – Ongoing`;
      return `<tr>
        <td>${escapeHtml(period)}</td>
        <td>${Number(schedule.car_first_10000_pence)}p</td>
        <td>${Number(schedule.car_over_10000_pence)}p</td>
        <td>${Number(schedule.motorcycle_pence)}p</td>
        <td>${Number(schedule.bicycle_pence)}p</td>
        <td>${Number(schedule.passenger_pence)}p</td>
      </tr>`;
    }).join('')
    : '<tr><td colspan="6" class="empty-state">No rate schedules have been configured.</td></tr>';
}

/** Load the effective-date rate history for the administrator page. */
async function loadMileageRateSchedules() {
  const data = await api('/admin/mileage-rates');
  renderMileageRateSchedules(data.items || []);
}

/** Save one complete rate schedule and refresh its derived effective periods. */
function attachMileageRateForm() {
  const form = document.getElementById('mileage-rate-form');
  const message = document.getElementById('mileage-rate-message');
  if (!form || !message) return;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    message.hidden = true;
    message.classList.remove('alert-danger', 'alert-success');
    const formData = new FormData(form);
    // Number inputs are sent as JSON integers to match the server's whole-pence contract.
    const payload = Object.fromEntries(formData.entries());
    [
      'car_first_10000_pence',
      'car_over_10000_pence',
      'motorcycle_pence',
      'bicycle_pence',
      'passenger_pence',
    ].forEach((field) => {
      payload[field] = Number(payload[field]);
    });
    try {
      await api('/admin/mileage-rates', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      form.reset();
      message.textContent = 'Rate schedule saved. Trips will use this schedule according to their occurrence date.';
      message.classList.add('alert-success');
      message.hidden = false;
      await loadMileageRateSchedules();
    } catch (error) {
      message.textContent = error.message || 'Unable to save rate schedule.';
      message.classList.add('alert-danger');
      message.hidden = false;
    }
  });
}

/** Reuse the loaded people list to populate person pickers on this page. */
function fillPersonSelects() {
  const options = state.people
    .map((person) => `<option value="${person.id}">${escapeHtml(person.name || 'Unknown')}</option>`)
    .join('');
  [
    'trip-person-select',
    'donation-person-select',
    'donation-edit-person-select',
    'payment-person-select',
    'declaration-person-select',
    'user-person-select',
  ].forEach((id) => {
    const select = document.getElementById(id);
    if (select) select.innerHTML = options || '<option value="">No people available</option>';
  });
}

/** Render the people directory, including management actions only for staff. */
function renderPeople() {
  const body = document.getElementById('people-table-body');
  if (!body) return;
  const canManage = appRole === Role.ADMIN || appRole === Role.TREASURER;
  const count = document.getElementById('people-count-label');
  if (count) count.textContent = `${state.people.length} record${state.people.length === 1 ? '' : 's'}`;
  body.innerHTML = state.people.length
    ? state.people.map((person) => `
      <tr>
        <td><span class="fw-semibold">${escapeHtml(person.name || '—')}</span></td>
        <td>${escapeHtml(person.role || '—')}</td>
        <td>${escapeHtml(person.email || '—')}</td>
        <td>${escapeHtml(person.phone || '—')}</td>
        <td><span class="status-badge ${person.active ? '' : 'inactive'}">${person.active ? 'Active' : 'Inactive'}</span></td>
        ${canManage ? `<td class="text-end text-nowrap">
          <button class="btn btn-sm btn-light border me-1" data-edit-person="${person.id}" aria-label="Edit ${escapeHtml(person.name)}">Edit</button>
          <button class="btn btn-sm btn-outline-danger" data-delete-person="${person.id}" aria-label="Anonymize ${escapeHtml(person.name)}">Anonymize</button>
        </td>` : ''}
      </tr>`).join('')
    : `<tr><td colspan="${canManage ? 6 : 5}" class="empty-state">No people found.</td></tr>`;
  applyTableSearch('people-table-body');
}

/** Render trip history and hide administrative actions from volunteers. */
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
        <td>${escapeHtml(trip.tax_year || '—')}</td>
        <td>${escapeHtml(trip.vehicle_type || '—')}</td>
        <td>${Number(trip.total_miles || 0).toFixed(1)}</td>
        <td>${trip.declaration_accepted ? escapeHtml(trip.declaration_text) : 'Not accepted'}</td>
        <td>${escapeHtml(trip.status || '—')}</td>
        ${canManage ? `<td><button class="btn btn-sm btn-outline-danger" data-delete-trip="${trip.id}">Delete</button></td>` : ''}
      </tr>`).join('')
    : `<tr><td colspan="${canManage ? 10 : 9}" class="text-center text-muted">No trips found</td></tr>`;
  applyTableSearch('trips-table-body');
}

/** Render declaration metadata without exposing unnecessary address details. */
function renderDeclarations() {
  const body = document.getElementById('declarations-table-body');
  if (!body) return;
  body.innerHTML = state.declarations.length
    ? state.declarations.map((entry) => `
      <tr>
        <td>${personName(entry.person_id)}</td>
        <td>${escapeHtml(entry.full_name)}</td>
        <td>${escapeHtml(entry.postcode || '—')}</td>
        <td>${dateLabel(entry.declaration_date)}</td>
        <td>${escapeHtml(entry.scope || '—')}</td>
        <td>${dateLabel(entry.withdrawn_date)}</td>
        <td><span class="status-badge ${entry.active ? '' : 'inactive'}">${entry.active ? 'Active' : 'Dormant'}</span></td>
        <td class="text-end text-nowrap">
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
        <td>${escapeHtml(entry.method || '—')}</td>
        <td>${entry.linked_payment_ids?.length
          ? entry.linked_payment_ids.map((paymentId) => `Payment #${Number(paymentId)}`).join(', ')
          : '—'}</td>
        <td>${entry.gift_aid_eligible ? 'Yes' : 'No'}</td>
        <td><span class="status-badge ${entry.active ? '' : 'inactive'}">${entry.active ? 'Active' : 'Dormant'}</span></td>
        <td>${escapeHtml(entry.created_by || 'Unknown')}</td>
        <td>${escapeHtml(entry.created_at || '—')}</td>
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
        <td>${escapeHtml(entry.method || '—')}</td>
        <td>${escapeHtml(entry.reference || '—')}</td>
        <td>${entry.linked_trips?.length
          ? entry.linked_trips.map((trip) => `Trip #${trip.id} · ${dateLabel(trip.trip_date)} · ${escapeHtml(trip.destination_name || 'Unknown destination')}`).join('<br>')
          : '—'}</td>
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
        <td>${Number(entry.payment_count)} payment${entry.payment_count === 1 ? '' : 's'} · ${Number(entry.linked_donation_count)} linked donation${entry.linked_donation_count === 1 ? '' : 's'}</td>
      </tr>`).join('')
    : '<tr><td colspan="5" class="empty-state">No payments have been recorded yet.</td></tr>';
  applyTableSearch('reconciliation-table-body');
}

/** Render account metadata returned by the admin-only users endpoint. */
function renderUsers() {
  const body = document.getElementById('users-table-body');
  if (!body) return;
  body.innerHTML = state.users.length
    ? state.users.map((user) => `
      <tr>
        <td>${escapeHtml(user.username)}</td>
        <td>${escapeHtml(user.role)}</td>
        <td>${user.person_id ? personName(user.person_id) : '—'}</td>
        <td>${user.is_active ? 'Active' : 'Inactive'}</td>
      </tr>`).join('')
    : '<tr><td colspan="4" class="text-center text-muted">No accounts found</td></tr>';
  applyTableSearch('users-table-body');
}

/** Render the latest audit events, escaping each field supplied by the API. */
function renderAudit() {
  const body = document.getElementById('audit-table-body');
  if (!body) return;
  body.innerHTML = state.audit.length
    ? state.audit.map((event) => `
      <tr>
        <td>${escapeHtml(event.occurred_at || '—')}</td>
        <td>${escapeHtml(event.actor_username || 'Unknown')}</td>
        <td>${escapeHtml(event.action)}</td>
        <td>${escapeHtml(event.entity_type)}</td>
        <td>${escapeHtml(event.entity_id)}</td>
        <td>${escapeHtml((event.changed_fields || []).join(', ') || '—')}</td>
      </tr>`).join('')
    : '<tr><td colspan="6" class="empty-state">No audit activity has been recorded.</td></tr>';
  applyTableSearch('audit-table-body');
}

/** Fetch people, refresh all page selectors, then redraw the people table. */
async function loadPeople() {
  const data = await api('/people');
  state.people = data.items || [];
  fillPersonSelects();
  renderPeople();
}

/** Fetch trip records and refresh the unpaid-trip payment picker. */
async function loadTrips() {
  const data = await api('/trips');
  state.trips = data.items || [];
  renderTrips();
  fillPaymentTripSelect();
}

/** Fetch declarations and update both the table and donation selectors. */
async function loadDeclarations() {
  const data = await api('/declarations');
  state.declarations = data.items || [];
  fillDeclarationSelect();
  renderDeclarations();
}

/** Offer available declarations in both the add and edit donation forms. */
function fillDeclarationSelect() {
  ['donation-declaration-select', 'donation-edit-declaration-select'].forEach((id) => {
    const select = document.getElementById(id);
    if (!select) return;
    select.innerHTML = '<option value="">No declaration linked</option>' + state.declarations
      .filter((entry) => entry.active)
      .map((entry) => `<option value="${entry.id}">${escapeHtml(entry.full_name)} · ${escapeHtml(entry.scope || '')}</option>`)
      .join('');
  });
}

/** Filter payment choices to match each donation form's selected person. */
function fillPaymentSelect() {
  [
    ['donation-person-select', 'donation-payment-select'],
    ['donation-edit-person-select', 'donation-edit-payment-select'],
  ].forEach(([personId, paymentId]) => {
    const select = document.getElementById(paymentId);
    if (!select) return;
    const selectedIds = new Set(Array.from(select.selectedOptions, (option) => option.value));
    const selectedPersonId = Number(document.getElementById(personId)?.value);
    const payments = state.payments.filter((payment) => payment.person_id === selectedPersonId);
    select.innerHTML = payments
      .map((payment) => {
        const label = `Payment #${payment.id} · ${money(payment.amount_pence)} · ${dateLabel(payment.paid_date)}`;
        return `<option value="${payment.id}" ${selectedIds.has(String(payment.id)) ? 'selected' : ''}>${escapeHtml(label)}</option>`;
      })
      .join('');
  });
}

/** Offer only this person's trips that have not already been paid. */
function fillPaymentTripSelect() {
  const select = document.getElementById('payment-trip-select');
  if (!select) return;
  const personId = Number(document.getElementById('payment-person-select')?.value);
  const trips = state.trips.filter((trip) => trip.person_id === personId && !trip.payment_id);
  select.innerHTML = trips.length
    ? trips.map((trip) => `<option value="${trip.id}">${dateLabel(trip.trip_date)} · ${escapeHtml(trip.destination_name || 'Unknown destination')} · ${Number(trip.total_miles || 0).toFixed(1)} miles</option>`).join('')
    : '<option value="" disabled>No unpaid trips for this person</option>';
  updatePaymentAmount();
}

/**
 * Sum the official reimbursement calculation for selected trips.
 * A second request sequence protects the amount from out-of-order responses.
 */
async function updatePaymentAmount() {
  const select = document.getElementById('payment-trip-select');
  const amount = document.getElementById('payment-amount');
  const help = document.getElementById('payment-trip-help');
  if (!select || !amount || !help) return;
  const requestSequence = ++paymentAmountRequestSequence;
  const trips = Array.from(select.selectedOptions)
    .map((option) => state.trips.find((trip) => trip.id === Number(option.value)))
    .filter(Boolean);
  if (!trips.length) {
    amount.readOnly = false;
    amount.value = '';
    help.textContent = 'Select one or more unpaid trips for this person. A trip can only be linked to one payment.';
    return;
  }
  amount.readOnly = true;
  help.textContent = 'Calculating the full reimbursement for selected trips…';
  try {
    const amounts = await Promise.all(trips.map((trip) => {
      const query = new URLSearchParams({
        trip_date: trip.trip_date,
        vehicle_type: trip.vehicle_type,
        miles_one_way: String(trip.miles_one_way),
        return_trip: String(Boolean(trip.return_trip)),
        passengers: String(trip.passengers || 0),
      });
      return api(`/api/calculate?${query.toString()}`);
    }));
    if (requestSequence !== paymentAmountRequestSequence) return;
    const total = amounts.reduce((sum, item) => sum + Number(item.total_amount_pence || 0), 0);
    amount.value = String(total);
    help.textContent = `Full reimbursement total: ${money(total)} for ${trips.length} selected trip${trips.length === 1 ? '' : 's'}.`;
  } catch (error) {
    if (requestSequence !== paymentAmountRequestSequence) return;
    amount.value = '';
    help.textContent = error.message || 'Could not calculate the selected trips. Clear the selection or try again.';
  }
}

/** Fetch donation records and redraw their table. */
async function loadDonations() {
  const data = await api('/donations');
  state.donations = data.items || [];
  renderDonations();
}

/** Fetch payments, then refresh donation choices and the payment table. */
async function loadPayments() {
  const data = await api('/payments');
  state.payments = data.items || [];
  fillPaymentSelect();
  renderPayments();
}

/** Fetch the finance summary for the current Payments page. */
async function loadPaymentReconciliation() {
  const data = await api('/reports/payment-reconciliation');
  state.reconciliation = data.items || [];
  renderPaymentReconciliation();
}

/** Fetch and display club totals and per-volunteer report rows. */
async function loadFinancialReport() {
  const report = await api('/reports/financial');
  document.getElementById('report-expenses-paid').textContent = money(report.totals.expenses_paid_pence);
  document.getElementById('report-donations').textContent = money(report.totals.donations_pence);
  document.getElementById('report-gift-aid').textContent = money(report.totals.gift_aid_pence);
  fillReportVolunteerFilters();
  const body = document.getElementById('financial-report-table-body');
  body.innerHTML = report.volunteers.length
    ? report.volunteers.map((volunteer) => `
      <tr data-person-id="${Number(volunteer.person_id)}">
        <td>${escapeHtml(volunteer.person_name)}</td>
        <td class="text-end">${Number(volunteer.trip_count)}</td>
        <td class="text-end">${Number(volunteer.total_miles).toFixed(1)}</td>
        <td class="text-end">${money(volunteer.expenses_paid_pence)}</td>
        <td class="text-end">${money(volunteer.donations_pence)}</td>
        <td class="text-end">${money(volunteer.difference_pence)}</td>
      </tr>`).join('')
    : '<tr><td colspan="6" class="empty-state" data-empty-row>No volunteer records found.</td></tr>';
  filterVolunteerSummary();
}

/** Fill volunteer controls from the people directory while preserving selections. */
function fillReportVolunteerFilters() {
  const volunteers = state.people
    .filter((person) => person.role === Role.VOLUNTEER)
    .sort((left, right) => left.name.localeCompare(right.name));
  const controls = [
    document.getElementById('summary-volunteer-filter'),
    ...Array.from(document.querySelectorAll('[data-filter-field="person"]')),
  ].filter(Boolean);
  controls.forEach((select) => {
    const selected = select.value;
    select.innerHTML = '<option value="">All volunteers</option>'
      + volunteers.map((person) => `<option value="${Number(person.id)}">${escapeHtml(person.name)}</option>`).join('');
    if (volunteers.some((person) => String(person.id) === selected)) select.value = selected;
  });
}

/** Refresh dynamic report filter choices without clearing an active selection. */
function fillReportValueFilter(selectId, values, placeholder) {
  const select = document.getElementById(selectId);
  if (!select) return;
  const selected = select.value;
  const uniqueValues = [...new Set(values.filter(Boolean))].sort((left, right) => left.localeCompare(right));
  select.innerHTML = `<option value="">${escapeHtml(placeholder)}</option>`
    + uniqueValues.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join('');
  if (uniqueValues.includes(selected)) select.value = selected;
}

/** Format integer pence as a plain decimal value suitable for spreadsheet CSV. */
function csvPounds(value) {
  return (Number(value || 0) / 100).toFixed(2);
}

/** Render the filterable report tables from the existing finance APIs. */
async function loadReportDataTables() {
  const [tripData, donationData, paymentData] = await Promise.all([
    api('/trips'),
    api('/donations'),
    api('/payments'),
  ]);
  fillReportValueFilter('trips-tax-year-filter', tripData.items.map((trip) => trip.tax_year || ''), 'All tax years');
  fillReportValueFilter('donations-method-filter', donationData.items.map((donation) => donation.method || ''), 'All methods');
  fillReportValueFilter('payments-method-filter', paymentData.items.map((payment) => payment.method || ''), 'All methods');
  const tripsBody = document.getElementById('trips-report-table-body');
  tripsBody.innerHTML = tripData.items.length
    ? tripData.items.map((trip) => `
      <tr data-report-date="${escapeHtml(trip.trip_date || '')}" data-person-id="${Number(trip.person_id)}" data-vehicle="${escapeHtml(trip.vehicle_type || '')}" data-tax-year="${escapeHtml(trip.tax_year || '')}" data-status="${escapeHtml(trip.status || '')}">
        <td data-csv-value="${escapeHtml(trip.trip_date || '')}">${dateLabel(trip.trip_date)}</td>
        <td>${personName(trip.person_id)}</td>
        <td>${escapeHtml(trip.origin_name || 'Legacy trip / origin not set')}</td>
        <td>${escapeHtml(trip.destination_name || 'Legacy trip / destination not set')}</td>
        <td>${escapeHtml(trip.tax_year || '—')}</td>
        <td>${escapeHtml(trip.vehicle_type || '—')}</td>
        <td data-csv-value="${Number(trip.total_miles || 0)}">${Number(trip.total_miles || 0).toFixed(1)}</td>
        <td>${escapeHtml(trip.status || '—')}</td>
      </tr>`).join('')
    : '<tr><td colspan="8" class="empty-state" data-empty-row>No trips recorded.</td></tr>';

  const donationsBody = document.getElementById('donations-report-table-body');
  donationsBody.innerHTML = donationData.items.length
    ? donationData.items.map((donation) => `
      <tr data-report-date="${escapeHtml(donation.donated_date || '')}" data-person-id="${Number(donation.person_id)}" data-eligible="${donation.gift_aid_eligible ? 'yes' : 'no'}" data-status="${donation.active ? 'active' : 'dormant'}" data-method="${escapeHtml(donation.method || '')}">
        <td data-csv-value="${escapeHtml(donation.donated_date || '')}">${dateLabel(donation.donated_date)}</td>
        <td>${personName(donation.person_id)}</td>
        <td data-csv-value="${csvPounds(donation.amount_pence)}">${money(donation.amount_pence)}</td>
        <td data-csv-value="${escapeHtml(donation.received_date || '')}">${dateLabel(donation.received_date)}</td>
        <td>${escapeHtml(donation.method || '—')}</td>
        <td>${(donation.linked_payment_ids || []).map((id) => `Payment #${Number(id)}`).join(', ') || '—'}</td>
        <td>${donation.gift_aid_eligible ? 'Yes' : 'No'}</td>
        <td>${donation.active ? 'Active' : 'Dormant'}</td>
      </tr>`).join('')
    : '<tr><td colspan="8" class="empty-state" data-empty-row>No donations recorded.</td></tr>';

  const paymentsBody = document.getElementById('payments-report-table-body');
  paymentsBody.innerHTML = paymentData.items.length
    ? paymentData.items.map((payment) => `
      <tr data-report-date="${escapeHtml(payment.paid_date || '')}" data-person-id="${Number(payment.person_id)}" data-related-donations="${payment.linked_donation_count ? 'yes' : 'no'}" data-method="${escapeHtml(payment.method || '')}">
        <td data-csv-value="${escapeHtml(payment.paid_date || '')}">${dateLabel(payment.paid_date)}</td>
        <td>${personName(payment.person_id)}</td>
        <td data-csv-value="${csvPounds(payment.amount_pence)}">${money(payment.amount_pence)}</td>
        <td>${escapeHtml(payment.method || '—')}</td>
        <td>${escapeHtml(payment.reference || '—')}</td>
        <td>${(payment.linked_trips || []).map((trip) => `Trip #${Number(trip.id)} · ${dateLabel(trip.trip_date)}`).join('; ') || '—'}</td>
        <td data-csv-value="${csvPounds(payment.related_donation_amount_pence ?? payment.donated_back_pence)}">${money(payment.related_donation_amount_pence ?? payment.donated_back_pence)}${payment.linked_donation_count ? ` (${payment.linked_donation_count} related; reference only)` : ''}</td>
      </tr>`).join('')
    : '<tr><td colspan="7" class="empty-state" data-empty-row>No payments recorded.</td></tr>';

  ['trips', 'donations', 'payments'].forEach(filterReportTable);
}

/** Apply one report's date and field filters to its rows. */
function filterReportTable(reportKey) {
  const table = document.getElementById(`${reportKey}-report-table`);
  if (!table) return;
  const from = document.getElementById(`${reportKey}-date-from`).value;
  const to = document.getElementById(`${reportKey}-date-to`).value;
  const filters = Array.from(document.querySelectorAll(`[data-report-filter="${reportKey}"]`))
    .filter((select) => select.value)
    .map((select) => [select.dataset.filterField, select.value]);
  const rows = Array.from(table.tBodies[0].rows).filter((row) => !row.hasAttribute('data-empty-row'));
  const invalidRange = from && to && from > to;
  let visibleCount = 0;
  rows.forEach((row) => {
    const rowDate = row.dataset.reportDate || '';
    const visible = !invalidRange
      && (!from || rowDate >= from)
      && (!to || rowDate <= to)
      && filters.every(([field, value]) => {
        const rowValue = {
          person: row.dataset.personId,
          vehicle: row.dataset.vehicle,
          'tax-year': row.dataset.taxYear,
          status: row.dataset.status,
          eligible: row.dataset.eligible,
          'related-donations': row.dataset.relatedDonations,
          method: row.dataset.method,
        }[field];
        return rowValue === value;
      });
    row.hidden = !visible;
    if (visible) visibleCount += 1;
  });
  let emptyRow = table.querySelector('[data-empty-row]');
  if (!emptyRow) {
    emptyRow = document.createElement('tr');
    emptyRow.innerHTML = `<td colspan="${table.tHead.rows[0].cells.length}" class="empty-state" data-empty-row>No records match this date range.</td>`;
    table.tBodies[0].append(emptyRow);
  }
  emptyRow.hidden = visibleCount > 0;
  const hasFilters = Boolean(from || to || filters.length);
  emptyRow.cells[0].textContent = invalidRange
    ? 'The start date must be on or before the end date.'
    : rows.length && !visibleCount
      ? 'No records match the selected filters.'
      : `No ${reportKey} recorded.`;
  status.textContent = invalidRange
    ? 'Choose a start date on or before the end date.'
    : `${visibleCount} record${visibleCount === 1 ? '' : 's'} shown${hasFilters ? ' with the selected filters' : ''}.`;
}

/** Apply the volunteer selector to the per-volunteer financial summary. */
function filterVolunteerSummary() {
  const table = document.getElementById('financial-report-table');
  const select = document.getElementById('summary-volunteer-filter');
  const status = document.getElementById('summary-filter-status');
  if (!table || !select || !status) return;
  const rows = Array.from(table.tBodies[0].rows).filter((row) => !row.hasAttribute('data-empty-row'));
  let visibleCount = 0;
  rows.forEach((row) => {
    row.hidden = Boolean(select.value) && row.dataset.personId !== select.value;
    if (!row.hidden) visibleCount += 1;
  });
  let emptyRow = table.querySelector('[data-empty-row]');
  if (!emptyRow) {
    emptyRow = document.createElement('tr');
    emptyRow.innerHTML = '<td colspan="6" class="empty-state" data-empty-row>No volunteers match this filter.</td>';
    table.tBodies[0].append(emptyRow);
  }
  emptyRow.hidden = visibleCount > 0;
  emptyRow.cells[0].textContent = rows.length && !visibleCount
    ? 'No volunteers match this filter.'
    : 'No volunteer records found.';
  status.textContent = `${visibleCount} volunteer${visibleCount === 1 ? '' : 's'} shown.`;
}

/** Export the currently visible data rows of a report table as safe CSV. */
function exportReportTable(tableId, filenamePrefix) {
  const table = document.getElementById(tableId);
  if (!table) return;
  const rows = [
    Array.from(table.tHead.rows[0].cells, (cell) => cell.textContent.trim()),
    ...Array.from(table.tBodies[0].rows)
      .filter((row) => !row.hidden && !row.hasAttribute('data-empty-row'))
      .map((row) => Array.from(row.cells, (cell) => {
        let value = cell.dataset.csvValue ?? cell.textContent.trim();
        if (/^[\s]*[=+\-@]/.test(value)) value = `'${value}`;
        return value;
      })),
  ];
  const csv = rows.map((row) => row.map((value) => {
    const text = String(value ?? '');
    return `"${text.replace(/"/g, '""')}"`;
  }).join(',')).join('\r\n');
  const blob = new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `${filenamePrefix}-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

/** Fetch recent audit events for the admin-only audit page. */
async function loadAudit() {
  const data = await api('/admin/audit');
  state.audit = data.items || [];
  renderAudit();
}

/** Fetch accounts for the admin-only user directory. */
async function loadUsers() {
  const data = await api('/admin/users');
  state.users = data.items || [];
  renderUsers();
}

/**
 * Connect a create form to its POST endpoint, normalize form-specific values,
 * close the modal on success, and reload the visible page's data.
 */
function attachForm(formId, endpoint, afterSave = refreshPageData) {
  const form = document.getElementById(formId);
  if (!form) return;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(form).entries());
    if (formId === 'user-form' && payload.role === Role.ADMIN) delete payload.person_id;
    if (formId === 'donation-form') {
      payload.linked_payment_ids = Array.from(
        document.getElementById('donation-payment-select')?.selectedOptions || [],
        (option) => Number(option.value),
      );
    }
    if (formId === 'payment-form') {
      payload.trip_ids = Array.from(document.getElementById('payment-trip-select')?.selectedOptions || [])
        .map((option) => Number(option.value))
        .filter((tripId) => tripId > 0);
    }
    try {
      await api(endpoint, { method: 'POST', body: JSON.stringify(payload) });
      form.reset();
      if (formId === 'trip-form') {
        const declaration = document.getElementById('declaration-accepted');
        if (declaration) declaration.checked = false;
      }
      const modal = form.closest('.modal');
      if (modal) bootstrap.Modal.getOrCreateInstance(modal).hide();
      await afterSave();
    } catch (error) {
      alert(error.message || 'Unable to save record.');
    }
  });
}

/** Connect an edit form to PUT, preserving boolean checkbox values explicitly. */
function attachUpdateForm(formId, endpoint, afterSave = refreshPageData) {
  const form = document.getElementById(formId);
  if (!form) return;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const formData = new FormData(form);
    const payload = Object.fromEntries(formData.entries());
    const recordId = payload.id;
    delete payload.id;
    if (!recordId) return;
    ['active', 'home_ground', 'gift_aid_eligible'].forEach((field) => {
      const checkbox = form.elements.namedItem(field);
      if (checkbox instanceof HTMLInputElement && checkbox.type === 'checkbox') {
        payload[field] = checkbox.checked;
      }
    });
    if (formId === 'donation-edit-form') {
      payload.linked_payment_ids = Array.from(
        form.elements.namedItem('linked_payment_ids')?.selectedOptions || [],
        (option) => Number(option.value),
      );
    }
    try {
      await api(`${endpoint}/${recordId}`, {
        method: 'PUT',
        body: JSON.stringify(payload),
      });
      bootstrap.Modal.getOrCreateInstance(form.closest('.modal')).hide();
      await afterSave();
    } catch (error) {
      alert(error.message || 'Unable to update record.');
    }
  });
}

/** Upload a locations CSV without JSON-encoding its multipart form data. */
function attachDestinationImportForm() {
  const form = document.getElementById('destination-import-form');
  if (!form) return;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const headers = {
      'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
    };
    try {
      const response = await fetch('/destinations/import', {
        method: 'POST',
        headers,
        body: new FormData(form),
      });
      const data = (response.headers.get('content-type') || '').includes('application/json')
        ? await response.json()
        : await response.text();
      if (!response.ok) throw new Error(data.error || 'Unable to import locations.');
      const completed = response.status === 202 ? await waitForImport(data.status_url) : data;
      form.reset();
      bootstrap.Modal.getOrCreateInstance(form.closest('.modal')).hide();
      await refreshPageData();
      const skippedRows = completed.skipped_duplicate_rows || [];
      const skippedMessage = skippedRows.length
        ? ` ${skippedRows.length} duplicate CSV row${skippedRows.length === 1 ? '' : 's'} skipped: ${skippedRows.join(', ')}.`
        : '';
      alert(`${completed.imported} location${completed.imported === 1 ? '' : 's'} imported successfully.${skippedMessage}`);
    } catch (error) {
      alert(error.message || 'Unable to import locations.');
    }
  });
}

/** Upload trip CSV data while preserving the session CSRF token. */
function attachTripImportForm() {
  const form = document.getElementById('trip-import-form');
  if (!form) return;
  const errorContainer = document.getElementById('trip-import-errors');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (errorContainer) {
      errorContainer.hidden = true;
      errorContainer.replaceChildren();
    }
    const headers = {
      'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
    };
    try {
      const response = await fetch('/trips/import', {
        method: 'POST',
        headers,
        body: new FormData(form),
      });
      const data = (response.headers.get('content-type') || '').includes('application/json')
        ? await response.json()
        : await response.text();
      if (!response.ok) {
        if (errorContainer && Array.isArray(data.errors)) {
          const heading = document.createElement('p');
          heading.className = 'mb-2';
          heading.textContent = data.error || 'Some trip rows could not be imported:';
          const list = document.createElement('ul');
          list.className = 'mb-0';
          data.errors.forEach((message) => {
            const item = document.createElement('li');
            item.textContent = message;
            list.append(item);
          });
          errorContainer.replaceChildren(heading, list);
          errorContainer.hidden = false;
          return;
        }
        throw new Error(data.error || 'Unable to import trips.');
      }
      const completed = response.status === 202 ? await waitForImport(data.status_url) : data;
      form.reset();
      bootstrap.Modal.getOrCreateInstance(form.closest('.modal')).hide();
      await refreshPageData();
      alert(`${completed.imported} trip${completed.imported === 1 ? '' : 's'} imported successfully.`);
    } catch (error) {
      alert(error.message || 'Unable to import trips.');
    }
  });
}

/** Poll a bounded background import until its worker returns the final result. */
async function waitForImport(statusUrl) {
  for (;;) {
    const status = await api(statusUrl);
    if (status.status === 'completed') return status.result;
    if (status.status === 'failed') {
      const error = new Error(status.result?.error || 'The import could not be completed.');
      error.details = status.result?.errors || [];
      throw error;
    }
    // A short delay avoids hammering the server while a route lookup is busy.
    await new Promise((resolve) => window.setTimeout(resolve, 750));
  }
}

/** Filter visible table rows locally according to the paired search input. */
function applyTableSearch(tableBodyId) {
  const search = document.querySelector(`[data-table-search="${tableBodyId}"]`);
  const body = document.getElementById(tableBodyId);
  if (!search || !body) return;
  const query = search.value.trim().toLocaleLowerCase();
  Array.from(body.rows).forEach((row) => {
    row.hidden = query !== '' && !row.textContent.toLocaleLowerCase().includes(query);
  });
}

/** Load the page-specific API data after startup or a successful write. */
async function refreshPageData() {
  const page = document.body.dataset.page;
  await loadPeople();
  if (page === 'mileage_rates_page') return loadMileageRateSchedules();
  if (page === 'index') {
    if (appRole !== Role.VOLUNTEER) {
      const summary = await api('/reports/summary');
      renderSummary(summary);
    }
    const overview = await api('/dashboard/overview');
    renderDashboardOverview(overview);
    return;
  }
  if (page === 'destinations_page') return loadDestinations();
  if (page === 'trips_page') {
    await loadDestinations();
    return loadTrips();
  }
  if (page === 'declarations_page') return loadDeclarations();
  if (page === 'donations_page') {
    await loadDeclarations();
    await loadPayments();
    return loadDonations();
  }
  if (page === 'payments_page') {
    await loadTrips();
    await loadPayments();
    return loadPaymentReconciliation();
  }
  if (page === 'reports_page') {
    await loadFinancialReport();
    return loadReportDataTables();
  }
  if (page === 'users_page') return loadUsers();
  if (page === 'audit_page') return loadAudit();
}

/** Populate the person edit dialog from the already-loaded directory record. */
function editPerson(personId) {
  const person = state.people.find((entry) => entry.id === personId);
  if (!person) return;
  const form = document.getElementById('person-edit-form');
  if (!form) return;
  Object.entries(person).forEach(([key, value]) => {
    const field = form.elements.namedItem(key);
    if (!field) return;
    if (field.type === 'checkbox') field.checked = Boolean(Number(value));
    else if (key === 'active' && field.tagName === 'SELECT') field.value = Number(value) ? 'true' : 'false';
    else field.value = value ?? '';
  });
  bootstrap.Modal.getOrCreateInstance(document.getElementById('person-edit-modal')).show();
}

/** Populate the donation edit dialog and restore its person-specific links. */
function editDonation(donationId) {
  const donation = state.donations.find((entry) => entry.id === donationId);
  const form = document.getElementById('donation-edit-form');
  if (!donation || !form) return;
  Object.entries(donation).forEach(([key, value]) => {
    const field = form.elements.namedItem(key);
    if (!field) return;
    if (key === 'linked_payment_id' || key === 'linked_payment_ids') return;
    if (field.type === 'checkbox') field.checked = Boolean(Number(value));
    else field.value = value ?? '';
  });
  fillPaymentSelect();
  const paymentSelect = form.elements.namedItem('linked_payment_ids');
  const linkedPaymentIds = new Set((donation.linked_payment_ids || []).map(String));
  Array.from(paymentSelect.options).forEach((option) => {
    option.selected = linkedPaymentIds.has(option.value);
  });
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
async function removeRecord(path, message) {
  if (!window.confirm(message)) return;
  try {
    await api(path, { method: 'DELETE' });
    await refreshPageData();
  } catch (error) {
    alert(error.message || 'Unable to delete record.');
  }
}

// Wire form submission and delegated table actions once the page markup exists.
document.addEventListener('DOMContentLoaded', async () => {
  attachMileageRateForm();
  attachForm('person-form', '/people');
  attachUpdateForm('person-edit-form', '/people');
  attachForm('destination-form', '/destinations');
  attachDestinationImportForm();
  attachUpdateForm('destination-edit-form', '/destinations');
  attachForm('trip-form', '/trips');
  attachTripImportForm();
  attachForm('declaration-form', '/declarations');
  attachUpdateForm('declaration-edit-form', '/declarations');
  attachForm('donation-form', '/donations');
  attachUpdateForm('donation-edit-form', '/donations');
  attachForm('payment-form', '/payments');
  attachForm('user-form', '/admin/users');

  const userRole = document.getElementById('user-role-select');
  const personField = document.getElementById('user-person-field');
  if (userRole && personField) {
    const updateVisibility = () => personField.classList.toggle('d-none', userRole.value === Role.ADMIN);
    userRole.addEventListener('change', updateVisibility);
    updateVisibility();
  }
  document.getElementById('donation-person-select')?.addEventListener('change', fillPaymentSelect);
  document.getElementById('donation-edit-person-select')?.addEventListener('change', fillPaymentSelect);
  document.getElementById('payment-person-select')?.addEventListener('change', fillPaymentTripSelect);
  document.getElementById('payment-trip-select')?.addEventListener('change', updatePaymentAmount);
  document.getElementById('trip-origin-select')?.addEventListener('change', updateTripMileage);
  document.getElementById('trip-destination-select')?.addEventListener('change', updateTripMileage);
  document.querySelectorAll('[data-table-search]').forEach((search) => {
    search.addEventListener('input', () => applyTableSearch(search.dataset.tableSearch));
  });
  document.querySelectorAll('[data-report-date-filter]').forEach((filter) => {
    filter.addEventListener('input', () => filterReportTable(filter.dataset.reportDateFilter));
  });
  document.querySelectorAll('[data-report-filter]').forEach((filter) => {
    filter.addEventListener('change', () => filterReportTable(filter.dataset.reportFilter));
  });
  document.getElementById('summary-volunteer-filter')?.addEventListener('change', filterVolunteerSummary);
  document.querySelectorAll('[data-export-table]').forEach((button) => {
    button.addEventListener('click', () => {
      exportReportTable(button.dataset.exportTable, button.dataset.exportFilename);
    });
  });

  document.addEventListener('click', async (event) => {
    const editDeclarationButton = event.target.closest('[data-edit-declaration]');
    if (editDeclarationButton) return editDeclaration(Number(editDeclarationButton.dataset.editDeclaration));
    const toggleDeclarationButton = event.target.closest('[data-toggle-declaration]');
    if (toggleDeclarationButton) {
      try {
        const declarationId = toggleDeclarationButton.dataset.toggleDeclaration;
        const active = toggleDeclarationButton.dataset.active === 'true';
        if (active) {
          await api(`/declarations/${declarationId}`, {
            method: 'PUT',
            body: JSON.stringify({ active: true }),
          });
        } else {
          await api(`/declarations/${declarationId}`, { method: 'DELETE' });
        }
        await refreshPageData();
      } catch (error) {
        alert(error.message || 'Unable to update declaration status.');
      }
      return;
    }
    const editDonationButton = event.target.closest('[data-edit-donation]');
    if (editDonationButton) return editDonation(Number(editDonationButton.dataset.editDonation));
    const toggleDonationButton = event.target.closest('[data-toggle-donation]');
    if (toggleDonationButton) {
      try {
        await api(`/donations/${toggleDonationButton.dataset.toggleDonation}`, {
          method: 'PUT',
          body: JSON.stringify({ active: toggleDonationButton.dataset.active === 'true' }),
        });
        await refreshPageData();
      } catch (error) {
        alert(error.message || 'Unable to update donation status.');
      }
      return;
    }
    const editButton = event.target.closest('[data-edit-person]');
    if (editButton) return editPerson(Number(editButton.dataset.editPerson));
    const deletePersonButton = event.target.closest('[data-delete-person]');
    if (deletePersonButton) {
      return removeRecord(`/people/${deletePersonButton.dataset.deletePerson}`, 'Remove this person’s identifying details? Their trip distances and financial transaction history will be kept, but the person and their linked declarations will be anonymized. Any linked account will be deactivated.');
    }
    const deleteTripButton = event.target.closest('[data-delete-trip]');
    if (deleteTripButton) {
      return removeRecord(`/trips/${deleteTripButton.dataset.deleteTrip}`, 'Delete this trip record?');
    }
    const editDestinationButton = event.target.closest('[data-edit-destination]');
    if (editDestinationButton) return editDestination(Number(editDestinationButton.dataset.editDestination));
    const deleteDestinationButton = event.target.closest('[data-delete-destination]');
    if (deleteDestinationButton) {
      return removeRecord(`/destinations/${deleteDestinationButton.dataset.deleteDestination}`, 'Delete this destination? Destinations already used by trips cannot be deleted; deactivate them instead.');
    }
  });

  try {
    await refreshPageData();
  } catch (error) {
    alert(error.message || 'Unable to load this page.');
  }
});
