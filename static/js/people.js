// people browser behavior. Loaded as a classic script for CSP-safe cross-module bindings.

/**
 * Populate every person selector from the complete server-provided choice list.
 * Existing selections are restored after rebuilding the options, preventing a
 * background refresh from silently changing an in-progress form.
 */
/**
 * Populate person selectors from the complete server-provided choice collection.
 * Existing selections are restored where possible so a background refresh does
 * not unexpectedly change an administrator's partially completed form.
 */
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
    if (!select) return;
    const selected = select.value;
    select.innerHTML = options || '<option value="">No people available</option>';
    if (selected && state.people.some((person) => String(person.id) === selected)) {
      select.value = selected;
    }
  });
}

/** Render the people directory, including management actions only for staff. */

function renderPeople(total) {
  const body = document.getElementById('people-table-body');
  if (!body) return;
  const canManage = appRole === Role.ADMIN || appRole === Role.TREASURER;
  const count = document.getElementById('people-count-label');
  if (count) count.textContent = `${total} record${total === 1 ? '' : 's'}`;
  body.innerHTML = state.people.length
    ? state.people.map((person) => `
      <tr>
        <td>${Number(person.id)}</td>
        <td><span class="fw-semibold">${escapeHtml(person.name || '\u2014')}</span></td>
        <td>${escapeHtml(person.role || '\u2014')}</td>
        <td>${escapeHtml(person.email || '\u2014')}</td>
        <td>${escapeHtml(person.phone || '\u2014')}</td>
        <td><span class="status-badge ${person.active ? '' : 'inactive'}">${person.active ? 'Active' : 'Inactive'}</span></td>
        ${canManage ? `<td class="text-end text-nowrap">
          <button class="btn btn-sm btn-light border me-1" data-edit-person="${person.id}" aria-label="Edit ${escapeHtml(person.name)}">Edit</button>
          <button class="btn btn-sm btn-outline-danger" data-delete-person="${person.id}" aria-label="Anonymize ${escapeHtml(person.name)}">Anonymize</button>
        </td>` : ''}
      </tr>`).join('')
    : `<tr><td colspan="${canManage ? 7 : 6}" class="empty-state">No people found.</td></tr>`;
}

/** Render trip history and hide administrative actions from volunteers. */

async function loadPeople() {
  if (document.getElementById('people-table-body')) {
    return loadTablePage('people-table-body', '/people', (data) => {
      state.people = data.items;
      renderPeople(data.total);
    });
  }
  const data = await api('/people/choices');
  state.people = data.items || [];
  state.peopleById = new Map(state.people.map((person) => [person.id, person]));
  fillPersonSelects();
}

/** Fetch trip records and refresh the unpaid-trip payment picker. */

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


