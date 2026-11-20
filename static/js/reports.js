// reports browser behavior. Loaded as a classic script for CSP-safe cross-module bindings.

/**
 * Present club-wide financial totals returned by the reporting service.
 * Formatting is intentionally limited to display concerns because aggregation and
 * Gift Aid eligibility rules belong to the audited server-side query layer.
 */
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
            <li><span>${dateLabel(trip.trip_date)} \u00b7 ${escapeHtml(trip.destination_name || 'Destination not recorded')}</span><span class="status-badge">${escapeHtml(trip.status || 'submitted')}</span></li>
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
        ? `${start} \u2013 ${dateLabel(schedule.effective_to)}`
        : `${start} \u2013 Ongoing`;
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

async function loadFinancialReport() {
  const report = await api('/reports/financial');
  document.getElementById('report-expenses-paid').textContent = money(report.totals.expenses_paid_pence);
  document.getElementById('report-expenses-outstanding').textContent = money(report.totals.expenses_outstanding_pence);
  document.getElementById('report-donations').textContent = money(report.totals.donations_pence);
  document.getElementById('report-gift-aid').textContent = money(report.totals.gift_aid_pence);
  fillReportVolunteerFilters(report.volunteers);
  const body = document.getElementById('financial-report-table-body');
  body.innerHTML = report.volunteers.length
    ? report.volunteers.map((volunteer) => `
      <tr data-person-id="${Number(volunteer.person_id)}">
        <td>${escapeHtml(volunteer.person_name)}</td>
        <td>${escapeHtml((volunteer.person_type || 'volunteer').replace(/^./, (letter) => letter.toUpperCase()))}</td>
        <td class="text-end">${Number(volunteer.trip_count)}</td>
        <td class="text-end">${Number(volunteer.total_miles).toFixed(1)}</td>
        <td class="text-end">${money(volunteer.expenses_paid_pence)}</td>
        <td class="text-end">${money(volunteer.expenses_outstanding_pence)}</td>
        <td class="text-end">${money(volunteer.donations_pence)}</td>
        <td class="text-end">${money(volunteer.difference_pence)}</td>
      </tr>`).join('')
    : '<tr data-empty-row><td colspan="8" class="empty-state">No volunteer records found.</td></tr>';
  filterVolunteerSummary();
}

/** Fill volunteer controls from the people directory while preserving selections. */

function fillReportVolunteerFilters(volunteers) {
  const controls = [
    document.getElementById('summary-volunteer-filter'),
    ...Array.from(document.querySelectorAll('[data-filter-field="person"]')),
  ].filter(Boolean);
  controls.forEach((select) => {
    const selected = select.value;
    select.innerHTML = '<option value="">All volunteers</option>'
      + volunteers.map((person) => {
        const type = (person.person_type || 'volunteer').replace(/^./, (letter) => letter.toUpperCase());
        return `<option value="${Number(person.person_id)}">${escapeHtml(person.person_name)} (${escapeHtml(type)})</option>`;
      }).join('');
    if (volunteers.some((person) => String(person.person_id) === selected)) select.value = selected;
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

/** Load filter choices once, then let each section page through matching records. */

async function loadReportDataTables() {
  const options = await api('/reports/detail-options');
  fillReportValueFilter('trips-tax-year-filter', options.tax_years, 'All tax years');
  fillReportValueFilter('donations-method-filter', options.donation_methods, 'All methods');
  fillReportValueFilter('payments-method-filter', options.payment_methods, 'All methods');
  await Promise.all(['trips', 'donations', 'payments'].map(filterReportTable));
}

const reportRows = {
  trips: (trip) => `
      <tr>
        <td>${dateLabel(trip.trip_date)}</td>
        <td>${escapeHtml(trip.person_name)}</td>
        <td>${escapeHtml(trip.origin_name || 'Legacy trip / origin not set')}</td>
        <td>${escapeHtml(trip.destination_name || 'Legacy trip / destination not set')}</td>
        <td>${escapeHtml(trip.tax_year || '\u2014')}</td>
        <td>${escapeHtml(trip.vehicle_type || '\u2014')}</td>
        <td>${Number(trip.total_miles || 0).toFixed(1)}</td>
        <td>${money(trip.expense_amount_pence)}</td>
        <td>${trip.payment_id ? `Paid${trip.paid_date ? ` ${dateLabel(trip.paid_date)}` : ''}` : 'Unpaid'}</td>
        <td>${trip.matching_donation_received
          ? `Yes (${money(trip.matching_donation_amount_pence)})`
          : trip.matching_donation_linked ? 'Linked, awaiting receipt' : 'No'}</td>
        <td>${escapeHtml(trip.status || '\u2014')}</td>
      </tr>`,
  donations: (donation) => `
      <tr>
        <td>${dateLabel(donation.donated_date)}</td>
        <td>${escapeHtml(donation.person_name)}</td>
        <td>${money(donation.amount_pence)}</td>
        <td>${dateLabel(donation.received_date)}</td>
        <td>${escapeHtml(donation.method || '\u2014')}</td>
        <td>${donation.linked_payment_ids.map((id) => `Payment #${Number(id)}`).join(', ') || '\u2014'}</td>
        <td>${donation.gift_aid_eligible ? 'Yes' : 'No'}</td>
        <td>${donation.active ? 'Active' : 'Dormant'}</td>
      </tr>`,
  payments: (payment) => `
      <tr>
        <td>${dateLabel(payment.paid_date)}</td>
        <td>${escapeHtml(payment.person_name)}</td>
        <td>${money(payment.amount_pence)}</td>
        <td>${escapeHtml(payment.method || '\u2014')}</td>
        <td>${escapeHtml(payment.reference || '\u2014')}</td>
        <td>${payment.linked_trips.map((trip) => `Trip #${Number(trip.id)} \u00b7 ${dateLabel(trip.trip_date)}`).join('; ') || '\u2014'}</td>
        <td>${money(payment.related_donation_amount_pence)}${payment.linked_donation_count ? ` (${payment.linked_donation_count} related; reference only)` : ''}</td>
      </tr>`,
};

/** Read the filter selections shared by the table and its complete CSV export. */
function reportFilters(reportKey) {
  const fields = [
    ...document.querySelectorAll(`[data-report-filter="${reportKey}"]`),
    ...document.querySelectorAll(`[data-report-date-filter="${reportKey}"]`),
  ];
  return Object.fromEntries(fields
    .filter((field) => field.value)
    .map((field) => [field.dataset.filterField || field.dataset.dateBound, field.value]));
}

/** Render a fresh page for one report section after a filter changes. */

async function filterReportTable(reportKey) {
  const table = document.getElementById(`${reportKey}-report-table`);
  const status = document.getElementById(`${reportKey}-filter-status`);
  const body = table?.tBodies[0];
  if (!body || !status) return;
  const filters = reportFilters(reportKey);
  const hasFilters = Object.keys(filters).length > 0;
  try {
    status.textContent = 'Loading records\u2026';
    await loadTablePage(body.id, `/reports/data/${reportKey}`, (data) => {
      const empty = hasFilters ? 'No records match the selected filters.' : `No ${reportKey} recorded.`;
      body.innerHTML = data.items.length ? data.items.map(reportRows[reportKey]).join('')
        : `<tr data-empty-row><td colspan="${table.tHead.rows[0].cells.length}" class="empty-state">${empty}</td></tr>`;
      status.textContent = `${data.total} record${data.total === 1 ? '' : 's'} found.`;
    }, filters, true);
  } catch (error) {
    body.innerHTML = '';
    status.textContent = error.message;
  }
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
  let emptyRow = table.tBodies[0].querySelector('tr[data-empty-row]');
  if (!emptyRow) {
    emptyRow = document.createElement('tr');
    emptyRow.setAttribute('data-empty-row', '');
    emptyRow.innerHTML = '<td colspan="8" class="empty-state"></td>';
    table.tBodies[0].append(emptyRow);
  }
  emptyRow.hidden = visibleCount > 0;
  emptyRow.cells[0].textContent = rows.length && !visibleCount
    ? 'No volunteers match this filter.'
    : 'No volunteer records found.';
  status.textContent = `${visibleCount} volunteer${visibleCount === 1 ? '' : 's'} shown.`;
}

/** Export all rows matching the current section's filters. */

function exportReportTable(tableId, filenamePrefix) {
  const table = document.getElementById(tableId);
  if (!table) return;
  const filters = reportFilters(filenamePrefix);
  if (filters.from && filters.to && filters.from > filters.to) return;
  window.location.assign(`/reports/data/${filenamePrefix}/csv?${new URLSearchParams(filters)}`);
}

/** Fetch recent audit events for the admin-only audit page. */


