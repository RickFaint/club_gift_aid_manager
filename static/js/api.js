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

const state = { people: [], trips: [], tripChoices: [], destinations: [], declarations: [], declarationChoices: [], donations: [], payments: [], paymentChoices: [], reconciliation: [], users: [], audit: [] };
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

const tablePages = new Map();

/** Load one table page with stable navigation and server-side search. */
async function loadTablePage(bodyId, endpoint, render, filters = {}, reset = false) {
  const body = document.getElementById(bodyId);
  if (!body) return;
  let page = tablePages.get(bodyId);
  if (!page) {
    const search = document.querySelector(`[data-table-search="${bodyId}"]`);
    const navigation = document.createElement('div');
    navigation.className = 'd-flex align-items-center justify-content-between gap-2 flex-wrap px-3 py-2';
    navigation.innerHTML = '<small class="text-muted" role="status"></small><div class="d-flex gap-2">'
      + '<button type="button" class="btn btn-sm btn-outline-secondary">Previous</button>'
      + '<button type="button" class="btn btn-sm btn-outline-secondary">Next</button></div>';
    navigation.hidden = true;
    body.closest('.table-responsive').after(navigation);
    const [previous, next] = navigation.querySelectorAll('button');
    page = { offset: 0, version: 0, limit: 50, search, navigation, previous, next };
    tablePages.set(bodyId, page);
    const move = (pages) => {
      page.offset = Math.max(0, page.offset + pages * page.limit);
      page.reload().catch((error) => alert(error.message));
    };
    previous.addEventListener('click', () => move(-1));
    next.addEventListener('click', () => move(1));
    if (search) {
      search.dataset.serverSearch = 'true';
      search.maxLength = 200;
      let debounce;
      search.addEventListener('input', () => {
        page.version += 1;
        page.offset = 0;
        window.clearTimeout(debounce);
        debounce = window.setTimeout(() => page.reload().catch((error) => alert(error.message)), 200);
      });
    }
  }
  page.reload = () => loadTablePage(bodyId, endpoint, render, filters);
  if (reset) page.offset = 0;
  const version = ++page.version;
  const params = new URLSearchParams({ ...filters, limit: page.limit, offset: page.offset });
  if (page.search?.value.trim()) params.set('q', page.search.value.trim());
  page.previous.disabled = page.next.disabled = true;
  const data = await api(`${endpoint}?${params}`);
  if (version !== page.version) return;
  if (page.offset && page.offset >= data.total) {
    page.offset = Math.floor(Math.max(data.total - 1, 0) / page.limit) * page.limit;
    return page.reload();
  }
  render(data);
  page.navigation.hidden = data.total <= page.limit;
  page.navigation.querySelector('[role="status"]').textContent =
    `${page.offset + 1}\u2013${page.offset + data.items.length} of ${data.total}`;
  page.previous.disabled = page.offset === 0;
  page.next.disabled = page.offset + data.items.length >= data.total;
}

/** Return every page of a scoped, bounded record selector. */
async function loadAllChoices(path, filters = {}) {
  const items = [];
  for (let offset = 0; ; offset = items.length) {
    const params = new URLSearchParams({ ...filters, limit: 200, offset });
    const data = await api(`${path}?${params}`);
    items.push(...data.items);
    if (!data.items.length || items.length >= data.total) return items;
  }
}
