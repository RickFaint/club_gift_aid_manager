// destinations browser behavior. Loaded as a classic script for CSP-safe cross-module bindings.

/**
 * Render the current page of locations and report the server-side result count.
 * All persisted text is escaped before insertion so imported location data cannot
 * become executable markup in an administrator's browser.
 */
function renderDestinations(total) {
  const body = document.getElementById('destinations-table-body');
  if (!body) return;
  const count = document.getElementById('destinations-count-label');
  if (count) count.textContent = `${total} location${total === 1 ? '' : 's'}`;
  body.innerHTML = state.destinations.length
    ? state.destinations.map((destination) => `
      <tr>
        <td><span class="fw-semibold">${escapeHtml(destination.name)}</span>${destination.home_ground ? '<span class="badge rounded-pill text-bg-success-subtle ms-2">Home</span>' : ''}</td>
        <td>${escapeHtml(destination.address || '\u2014')}</td>
        <td>${escapeHtml(destination.town_city || '\u2014')}</td>
        <td>${escapeHtml(destination.postcode || '\u2014')}</td>
        <td>${destination.home_ground ? '<span class="text-success fw-semibold">Yes</span>' : '<span class="text-muted">\u2014</span>'}</td>
        <td><span class="status-badge ${destination.active ? '' : 'inactive'}">${destination.active ? 'Active' : 'Inactive'}</span></td>
        <td class="text-end text-nowrap">
          <button class="btn btn-sm btn-light border me-1" data-edit-destination="${destination.id}" aria-label="Edit ${escapeHtml(destination.name)}">Edit</button>
          <button class="btn btn-sm btn-outline-danger" data-delete-destination="${destination.id}" aria-label="Delete ${escapeHtml(destination.name)}">Delete</button>
        </td>
      </tr>`).join('')
    : '<tr><td colspan="7" class="empty-state">No locations found. Add a location to get started.</td></tr>';
}

/** Populate trip origin/destination controls and choose the configured home ground. */

function fillDestinationSelect() {
  const active = state.destinations.filter((destination) => destination.active);
  const label = (destination) => [destination.name, destination.town_city, destination.postcode]
    .filter(Boolean).join(' \u00b7 ');
  const connectSearch = (kind, select, defaultDestination = null) => {
    const search = document.getElementById(`trip-${kind}-search`);
    const choices = document.getElementById(`trip-${kind}-options`);
    if (!search || !choices || !select) return;
    choices.innerHTML = active.map((destination) => `<option value="${escapeHtml(label(destination))}"></option>`).join('');
    const synchronize = () => {
      const match = active.find((destination) => label(destination).toLocaleLowerCase() === search.value.trim().toLocaleLowerCase());
      select.value = match ? String(match.id) : '';
      select.dispatchEvent(new Event('change', { bubbles: true }));
      search.setCustomValidity(match || !search.value ? '' : 'Select a location from the search results.');
    };
    search.addEventListener('input', synchronize);
    search.addEventListener('change', synchronize);
    if (defaultDestination) {
      search.value = label(defaultDestination);
      select.value = String(defaultDestination.id);
    }
  };
  const destinationSelect = document.getElementById('trip-destination-select');
  if (destinationSelect) {
    destinationSelect.innerHTML = '<option value="">Select a destination</option>' + active
      .map((destination) => `<option value="${destination.id}">${escapeHtml(destination.name)}</option>`)
      .join('');
    connectSearch('destination', destinationSelect);
  }
  const originSelect = document.getElementById('trip-origin-select');
  if (originSelect) {
    originSelect.innerHTML = '<option value="">Select an origin</option>' + active
      .map((destination) => `<option value="${destination.id}">${escapeHtml(destination.name)}</option>`)
      .join('');
    const homeGround = active.find((destination) => destination.home_ground);
    if (homeGround) originSelect.value = String(homeGround.id);
    connectSearch('origin', originSelect, homeGround);
  }
}

/**
 * Request a fresh road route when both trip endpoints are selected.
 * Sequence IDs discard stale replies if the user changes locations mid-request.
 */

/**
 * Request an authoritative road distance for the two selected saved locations.
 * The displayed mileage is advisory for the user; trip submission independently
 * recalculates it on the server and never trusts this browser field as evidence.
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

  status.textContent = 'Calculating road mileage\u2026';
  status.classList.remove('text-danger', 'text-success', 'text-muted');
  try {
    const result = await api('/api/route-distance', {
      method: 'POST',
      body: JSON.stringify({
        origin_id: Number(origin.value),
        destination_id: Number(destination.value),
      }),
    });
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

async function loadDestinations() {
  if (document.getElementById('destinations-table-body')) {
    return loadTablePage('destinations-table-body', '/destinations', (data) => {
      state.destinations = data.items;
      renderDestinations(data.total);
    });
  }
  const data = await api('/destinations/choices');
  state.destinations = data.items || [];
  fillDestinationSelect();
}

/** Format integer pence as a localized pound amount. */

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


