// shared browser behavior. Loaded as a classic script for CSP-safe cross-module bindings.

/**
 * Encode an arbitrary value for safe interpolation into an HTML text context.
 * Renderers use this helper for persisted and imported data; numeric identifiers
 * are still converted explicitly where they are used in trusted attributes.
 */
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[char]));
}

/**
 * Format an integer-pence value as locale-aware sterling for display only.
 * Financial calculations remain on the server; coercion here simply keeps empty
 * report cells predictable and never feeds a formatted value back into storage.
 */
function money(value) {
  return currencyFormatter.format((Number(value) || 0) / 100);
}

/**
 * Convert an ISO calendar date into the day-first label expected by UK users.
 * Invalid legacy text is escaped and shown unchanged rather than silently replaced,
 * allowing administrators to identify data that may require correction.
 */
function dateLabel(value) {
  if (!value) return '\u2014';
  const parsed = new Date(`${value}T00:00:00`);
  return Number.isNaN(parsed.getTime()) ? escapeHtml(value) : parsed.toLocaleDateString('en-GB');
}

/** Resolve a person ID against the in-memory people list for table labels. */

function personName(personId) {
  const person = state.peopleById?.get(personId);
  return escapeHtml(person ? person.name : 'Unknown');
}

/**
 * Re-enable submit controls after JavaScript has attached a form handler.
 * Forms begin disabled in markup so a partially loaded page cannot perform an
 * unintended native submission before CSRF-aware behavior is ready.
 */
function enableHandledForm(form) {
  form.querySelectorAll('[type="submit"]').forEach((button) => { button.disabled = false; });
}

/**
 * Find or create the live status region associated with a managed form.
 * A consistent accessible region lets validation, progress, and success messages
 * reach assistive technology without replacing the form's controls.
 */
function formStatus(form) {
  let status = form.querySelector('[data-form-status]');
  if (!status) {
    status = document.createElement('div');
    status.dataset.formStatus = '';
    status.className = 'alert mt-3 mb-0';
    status.hidden = true;
    status.setAttribute('role', 'alert');
    form.querySelector('.modal-body')?.append(status);
  }
  return status;
}

/**
 * Toggle the pending state and submit controls for an asynchronous form request.
 * Disabling every submit button prevents duplicate writes while retaining enough
 * state to restore all available actions after the request completes.
 */
function setFormPending(form, pending) {
  form.querySelectorAll('[type="submit"]').forEach((button) => { button.disabled = pending; });
  form.setAttribute('aria-busy', String(pending));
}

/**
 * Display a styled, accessible outcome message next to a managed form.
 * The message is assigned as text rather than HTML so server validation text cannot
 * introduce markup into the page.
 */
function showFormStatus(form, message, kind = 'danger') {
  const status = formStatus(form);
  status.textContent = message;
  status.className = `alert alert-${kind} mt-3 mb-0`;
  status.hidden = false;
}

/**
 * Surface a non-form-specific error in the page's shared status region.
 * This provides a visible alternative to transient dialogs for failures that affect
 * table loading or page-level actions.
 */
function showPageStatus(message) {
  const status = document.createElement('div');
  status.className = 'alert alert-warning';
  status.setAttribute('role', 'status');
  status.tabIndex = -1;
  status.textContent = message;
  document.getElementById('main-content')?.prepend(status);
  status.focus();
}

/**
 * Convert an optional declaration image to the JSON representation expected by Flask.
 *
 * File inputs cannot be represented directly in JSON. FileReader produces a data URL
 * such as `data:image/png;base64,...`; only the characters after the comma are stored,
 * while the MIME type is sent separately. These browser checks improve usability, but
 * the Flask route repeats them because browser-side validation is not a security boundary.
 */
async function declarationImagePayload(form, payload) {
  const file = form.elements.namedItem('evidence_image')?.files?.[0];
  if (!file) return;
  if (!['image/jpeg', 'image/png', 'image/webp', 'application/pdf'].includes(file.type)) {
    throw new Error('Use a PNG, JPEG, WebP, or PDF declaration file.');
  }
  if (file.size > 512 * 1024) throw new Error('The declaration file must be 512 KiB or smaller.');
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener('load', () => resolve(reader.result));
    reader.addEventListener('error', () => reject(new Error('The declaration file could not be read.')));
    reader.readAsDataURL(file);
  });
  payload.evidence_image_base64 = String(dataUrl).split(',', 2)[1];
  payload.evidence_image_mime = file.type;
  delete payload.evidence_image;
}

/**
 * Render the current account page with role, activation, MFA, and admin actions.
 * Usernames are escaped before interpolation, while numeric record identifiers are
 * explicitly converted before being placed in action-button data attributes.
 */
function renderUsers() {
  const body = document.getElementById('users-table-body');
  if (!body) return;
  body.innerHTML = state.users.length
    ? state.users.map((user) => `
      <tr>
        <td>${escapeHtml(user.username)}</td>
        <td>${escapeHtml(user.role)}</td>
        <td>${user.person_id ? personName(user.person_id) : '\u2014'}</td>
        <td>${user.is_active ? 'Active' : 'Inactive'}</td>
        <td>${[Role.ADMIN, Role.TREASURER].includes(user.role)
          ? (user.mfa_enabled ? 'Enabled' : 'Enrollment required')
          : 'Not required'}</td>
        <td>
          <button class="btn btn-sm btn-outline-primary me-1" data-edit-user="${Number(user.id)}">Edit account</button>
          ${user.is_active && user.must_change_password
            ? `<button class="btn btn-sm btn-outline-primary me-1" data-invite-user="${Number(user.id)}">Issue code</button>`
            : ''}
          <button class="btn btn-sm btn-outline-primary me-1" data-reset-password="${Number(user.id)}" data-reset-username="${escapeHtml(user.username)}">Reset password</button>
          ${user.mfa_enabled
            ? `<button class="btn btn-sm btn-outline-danger" data-reset-mfa="${Number(user.id)}">Reset MFA</button>`
            : ''}
        </td>
      </tr>`).join('')
    : '<tr><td colspan="6" class="text-center text-muted">No accounts found</td></tr>';
  applyTableSearch('users-table-body');
}

/** Render the latest audit events, escaping each field supplied by the API. */

function renderAudit() {
  const body = document.getElementById('audit-table-body');
  if (!body) return;
  body.innerHTML = state.audit.length
    ? state.audit.map((event) => `
      <tr>
        <td>${escapeHtml(event.occurred_at || '\u2014')}</td>
        <td>${escapeHtml(event.actor_username || 'Unknown')}</td>
        <td>${escapeHtml(event.action)}</td>
        <td>${escapeHtml(event.entity_type)}</td>
        <td>${escapeHtml(event.entity_id)}</td>
        <td>${escapeHtml((event.changed_fields || []).join(', ') || '\u2014')}</td>
      </tr>`).join('')
    : '<tr><td colspan="6" class="empty-state">No audit activity has been recorded.</td></tr>';
  applyTableSearch('audit-table-body');
}

/**
 * Fetch one server-filtered audit page and render it into the administrator table.
 * Pagination remains in the shared loader so the audit renderer handles only the
 * bounded records supplied for the current view.
 */
async function loadAudit() {
  return loadTablePage('audit-table-body', '/admin/audit', (data) => {
    state.audit = data.items;
    renderAudit();
  });
}

/** Fetch accounts for the admin-only user directory. */

async function loadUsers() {
  return loadTablePage('users-table-body', '/admin/users', (data) => {
    state.users = data.items;
    renderUsers();
  });
}

/**
 * Attach standardized create behavior to a JSON or multipart application form.
 * The handler normalizes optional blanks, supplies the session CSRF token through
 * the shared API client, and refreshes visible data only after a successful write.
 */
function attachForm(formId, endpoint, afterSave = refreshPageData) {
  const form = document.getElementById(formId);
  if (!form) return;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (form.getAttribute('aria-busy') === 'true') return;
    formStatus(form).hidden = true;
    const payload = Object.fromEntries(new FormData(form).entries());
    if (formId === 'declaration-form') {
      try {
        await declarationImagePayload(form, payload);
      } catch (error) {
        showFormStatus(form, error.message);
        return;
      }
    }
    if (formId === 'donation-form') {
      // Forms use human-friendly decimal pounds. The API and database use integer
      // pence so calculations never inherit binary floating-point rounding errors.
      payload.amount_pence = Math.round(Number(payload.amount_pounds) * 100);
      delete payload.amount_pounds;
    }
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
    const idempotent = ['trip-form', 'payment-form'].includes(formId);
    if (idempotent && !form.dataset.idempotencyKey) form.dataset.idempotencyKey = crypto.randomUUID();
    setFormPending(form, true);
    try {
      await api(endpoint, { method: 'POST', body: JSON.stringify(payload), headers: idempotent ? { 'Idempotency-Key': form.dataset.idempotencyKey } : {} });
      form.reset();
      delete form.dataset.idempotencyKey;
      if (formId === 'trip-form') {
        const declaration = document.getElementById('declaration-accepted');
        if (declaration) declaration.checked = false;
      }
      const modal = form.closest('.modal');
      if (modal) bootstrap.Modal.getOrCreateInstance(modal).hide();
      try {
        await afterSave();
      } catch {
        showPageStatus('Saved successfully, but the refreshed list could not be loaded. Reload the page to see the saved record.');
      }
    } catch (error) {
      showFormStatus(form, error.message || 'Unable to save record.');
    } finally {
      setFormPending(form, false);
    }
  });
  enableHandledForm(form);
}

/** Connect an edit form to PUT, preserving boolean checkbox values explicitly. */

function attachUpdateForm(formId, endpoint, afterSave = refreshPageData) {
  const form = document.getElementById(formId);
  if (!form) return;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (form.getAttribute('aria-busy') === 'true') return;
    formStatus(form).hidden = true;
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
      payload.amount_pence = Math.round(Number(payload.amount_pounds) * 100);
      delete payload.amount_pounds;
      payload.linked_payment_ids = Array.from(
        form.elements.namedItem('linked_payment_ids')?.selectedOptions || [],
        (option) => Number(option.value),
      );
    }
    setFormPending(form, true);
    try {
      await api(`${endpoint}/${recordId}`, {
        method: 'PUT',
        body: JSON.stringify(payload),
      });
      bootstrap.Modal.getOrCreateInstance(form.closest('.modal')).hide();
      try {
        await afterSave();
      } catch {
        showPageStatus('Changes were saved, but the refreshed list could not be loaded. Reload the page to see them.');
      }
    } catch (error) {
      showFormStatus(form, error.message || 'Unable to update record.');
    } finally {
      setFormPending(form, false);
    }
  });
  enableHandledForm(form);
}

/** Upload a locations CSV without JSON-encoding its multipart form data. */

async function waitForImport(statusUrl) {
  for (;;) {
    const status = await api(statusUrl);
    if (status.status === 'completed') return status.result;
    if (status.status === 'failed') {
      const error = new Error(status.result?.error || 'The import could not be completed.');
      error.details = status.result?.errors || [];
      error.detailCount = status.result?.errors_count ?? error.details.length;
      throw error;
    }
    // A short delay avoids hammering the server while a route lookup is busy.
    await new Promise((resolve) => window.setTimeout(resolve, 750));
  }
}

/** Upload a simple CSV import, wait for background validation, and refresh the page. */
function attachCsvImportForm(formId, endpoint, singularLabel) {
  const form = document.getElementById(formId);
  if (!form) return;
  const pluralLabel = singularLabel === 'person' ? 'people' : `${singularLabel}s`;
  const status = form.querySelector('[data-csv-status]');
  const submits = Array.from(form.querySelectorAll('[type="submit"]'));
  const file = form.elements.namedItem('file');
  const setStatus = (message, kind, details = [], detailCount = details.length) => {
    status.hidden = false;
    status.className = `alert alert-${kind} mt-3 mb-0`;
    status.textContent = message;
    if (details.length) {
      const list = document.createElement('ul');
      list.className = 'mb-0';
      details.slice(0, 20).forEach((detail) => {
        const item = document.createElement('li');
        item.textContent = detail;
        list.append(item);
      });
      status.append(list);
      if (detailCount > 20) status.append(`Showing the first 20 of ${detailCount} issues.`);
    }
  };
  file.addEventListener('change', () => { status.hidden = true; });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (file.disabled) return;
    const preview = event.submitter?.name === 'preview';
    const body = new FormData(form);
    const headers = {
      'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
    };
    submits.forEach((button) => { button.disabled = true; });
    file.disabled = true;
    setStatus(preview ? 'Checking the CSV file...' : 'Importing records...', 'info');
    try {
      const response = await fetch(preview ? `${endpoint}?preview=1` : endpoint, {
        method: 'POST',
        headers,
        body,
      });
      const data = (response.headers.get('content-type') || '').includes('application/json')
        ? await response.json() : null;
      if (!response.ok || !data) {
        const message = response.status === 413
          ? 'The upload exceeds the request size limit of 1 MiB.'
          : data?.error || `Unable to import ${pluralLabel}.`;
        const error = new Error(message);
        error.details = data?.errors || [];
        error.detailCount = data?.errors_count ?? error.details.length;
        throw error;
      }
      const completed = response.status === 202 ? await waitForImport(data.status_url) : data;
      const skipped = completed.skipped_duplicate_rows_count ?? completed.skipped_duplicate_rows?.length ?? 0;
      const skippedMessage = skipped ? ` Duplicate rows skipped: ${skipped}.` : '';
      if (preview) {
        setStatus(`${completed.valid_rows} valid rows. Select Upload to save them.${skippedMessage}`, 'success');
        return;
      }
      form.reset();
      bootstrap.Modal.getOrCreateInstance(form.closest('.modal')).hide();
      status.hidden = true;
      alert(`${completed.imported} ${completed.imported === 1 ? singularLabel : pluralLabel} imported successfully.${skippedMessage}`);
      try {
        await refreshPageData();
      } catch {
        alert('The import succeeded. Reload the page to see the new records.');
      }
    } catch (error) {
      setStatus(error.message || `Unable to import ${pluralLabel}.`, 'danger', error.details, error.detailCount);
    } finally {
      file.disabled = false;
      submits.forEach((button) => { button.disabled = false; });
    }
  });
  enableHandledForm(form);
}

/** Filter visible table rows locally according to the paired search input. */

function applyTableSearch(tableBodyId) {
  const search = document.querySelector(`[data-table-search="${tableBodyId}"]`);
  const body = document.getElementById(tableBodyId);
  if (!search || !body || search.dataset.serverSearch === 'true') return;
  const query = search.value.trim().toLocaleLowerCase();
  Array.from(body.rows).forEach((row) => {
    row.hidden = query !== '' && !row.textContent.toLocaleLowerCase().includes(query);
  });
}

/** Load the page-specific API data after startup or a successful write. */

async function removeRecord(path, message) {
  if (!window.confirm(message)) return;
  try {
    await api(path, { method: 'DELETE' });
    await refreshPageData();
  } catch (error) {
    alert(error.message || 'Unable to delete record.');
  }
}


