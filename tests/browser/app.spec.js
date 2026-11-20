const { test, expect } = require('@playwright/test');
const { createHmac } = require('node:crypto');

const password = 'Browser-Test-Password3!';
const mfaSecret = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP';
let adminLoginNumber = 0;

/**
 * Generate the current six-digit test authenticator code from a fixed base32 secret.
 * Keeping this small implementation in the browser suite exercises the real MFA
 * login flow without weakening production verification or depending on wall-clock fixtures.
 */
function totp(secret) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = '';
  for (const character of secret) bits += alphabet.indexOf(character).toString(2).padStart(5, '0');
  const key = Buffer.from((bits.match(/.{8}/g) || []).map((byte) => parseInt(byte, 2)));
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30_000)));
  const digest = createHmac('sha1', key).update(counter).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  const value = (digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000;
  return String(value).padStart(6, '0');
}

/**
 * Authenticate a seeded browser-test account and complete MFA when its role requires it.
 * The helper waits for each redirect so individual UI tests begin from a stable,
 * fully authenticated dashboard rather than duplicating login timing assumptions.
 */
async function login(page, username) {
  const selectedUsername = username || `browser-admin-${++adminLoginNumber}`;
  await page.goto('/login');
  await page.locator('#login-username').fill(selectedUsername);
  await page.locator('#login-password').fill(password);
  await page.getByRole('button', { name: 'Sign in' }).click();
  if (selectedUsername !== 'browser-volunteer') {
    await expect(page).toHaveURL('/mfa/challenge');
    await page.locator('#mfa-code').fill(totp(mfaSecret));
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  }
  await expect(page).toHaveURL('/');
}

test('modal form submits the session CSRF token and updates the page', async ({ page }) => {
  await login(page);
  await page.goto('/people/page');
  await page.getByRole('button', { name: /Add person/ }).click();
  await expect(page.locator('#person-modal')).toHaveClass(/show/);

  const requestPromise = page.waitForRequest((request) =>
    request.url().endsWith('/people') && request.method() === 'POST');
  await page.locator('#person-name').fill('Created in a browser');
  await page.locator('#person-email').fill('created@example.test');
  await page.getByRole('button', { name: 'Save person' }).click();
  const request = await requestPromise;

  const csrf = await page.locator('meta[name="csrf-token"]').getAttribute('content');
  expect(request.headers()['x-csrf-token']).toBe(csrf);
  await expect(page.locator('#person-modal')).not.toHaveClass(/show/);
  await expect(page.locator('#people-table-body')).toContainText('Created in a browser');
});

test('navigation is role-specific', async ({ browser }) => {
  const expectations = [
    ['browser-admin', ['Reports', 'Accounts', 'Audit log', 'Mileage rates'], true],
    ['browser-treasurer', ['Locations', 'Declarations', 'Donations', 'Payments'], false],
    ['browser-volunteer', [], false],
  ];
  for (const [username, roleLinks, isAdmin] of expectations) {
    const context = await browser.newContext();
    const page = await context.newPage();
    await login(page, username);
    for (const link of roleLinks) await expect(page.locator('nav').getByRole('link', { name: link, exact: true })).toBeVisible();
    await expect(page.locator('nav').getByRole('link', { name: 'Reports', exact: true })).toHaveCount(isAdmin ? 1 : 0);
    await expect(page.locator('nav').getByRole('link', { name: 'Accounts', exact: true })).toHaveCount(isAdmin ? 1 : 0);
    if (username === 'browser-volunteer') {
      await expect(page.locator('nav').getByRole('link', { name: 'Payments', exact: true })).toHaveCount(0);
      await expect(page.locator('nav').getByRole('link', { name: 'Locations', exact: true })).toHaveCount(0);
    }
    await context.close();
  }
});

test('enforced CSP blocks an injected inline script', async ({ page }) => {
  const response = await page.goto('/login');
  const csp = response.headers()['content-security-policy'];
  expect(csp).toContain("default-src 'self'");
  expect(csp).toContain("object-src 'none'");
  await page.locator('body').evaluate((body) => {
    const script = document.createElement('script');
    script.textContent = 'window.__inlineCspProbe = true';
    body.append(script);
  });
  await expect.poll(() => page.evaluate(() => window.__inlineCspProbe)).toBe(undefined);
});

test('table values are escaped and client-side search filters rows', async ({ page }) => {
  await login(page);
  await page.goto('/people/page');
  const body = page.locator('#people-table-body');
  await expect(body).toContainText('<img src=x onerror="window.__tableXss=true">');
  await expect(body.locator('img')).toHaveCount(0);
  expect(await page.evaluate(() => window.__tableXss)).toBe(undefined);

  await page.getByRole('searchbox', { name: 'Search people' }).fill('Browser Volunteer');
  await expect(body.locator('tr:visible')).toHaveCount(1);
  await expect(body.locator('tr:visible')).toContainText('Browser Volunteer');
});

test('CSV template downloads through the browser', async ({ page }) => {
  await login(page);
  await page.goto('/trips/page');
  await page.getByRole('button', { name: 'Upload CSV' }).click();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('link', { name: 'Download CSV template' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('trips-template.csv');
  const stream = await download.createReadStream();
  let content = '';
  for await (const chunk of stream) content += chunk.toString('utf8');
  expect(content).toContain('person_id,origin_id,destination_id,trip_date,vehicle_type');
});

test('route-distance requests are POSTed with CSRF and populate mileage', async ({ page }) => {
  await login(page);
  let captured;
  await page.route('**/api/route-distance', async (route) => {
    captured = route.request();
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ distance_miles: 12.4 }) });
  });
  await page.goto('/trips/page');
  await page.locator('#trip-origin-select').selectOption({ label: 'Club Ground' });
  await page.locator('#trip-destination-select').selectOption({ label: 'Away Ground' });
  await expect(page.locator('#trip-miles')).toHaveValue('12.4');

  expect(captured.method()).toBe('POST');
  expect(captured.headers()['x-csrf-token']).toBe(await page.locator('meta[name="csrf-token"]').getAttribute('content'));
  expect(captured.postDataJSON()).toEqual(expect.objectContaining({ origin_id: expect.any(Number), destination_id: expect.any(Number) }));
});

test('empty reports survive repeated filter changes', async ({ page }) => {
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(error.stack || error.message));
  await login(page);
  await page.goto('/reports/page');
  await expect(page.locator('#trips-report-table-body')).toContainText('No trips recorded.');

  const vehicleFilter = page.locator('#trips-vehicle-filter');
  for (const value of ['car', 'motorcycle', '', 'bicycle', '']) {
    await Promise.all([
      page.waitForResponse((response) => response.url().includes('/reports/data/trips?')),
      vehicleFilter.selectOption(value),
    ]);
    await expect(page.locator('#trips-filter-status')).toHaveText('0 records found.');
    await expect(page.locator('#trips-report-table-body tr[data-empty-row]')).toHaveCount(1);
  }

  expect(pageErrors).toEqual([]);
});

test('imported account can be edited and issued an activation code', async ({ page }) => {
  await login(page);
  await page.goto('/users/page');
  await page.getByRole('button', { name: 'Upload CSV' }).click();
  await page.locator('#accounts-csv-file').setInputFiles({
    name: 'accounts.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from('username,password,role,person_id\nbrowser-imported,,admin,\n'),
  });
  await page.locator('#accounts-import-form').getByRole('button', { name: 'Upload accounts' }).click();
  await expect(page.locator('#accounts-import-modal')).not.toHaveClass(/show/);

  const row = page.locator('#users-table-body tr', { hasText: 'browser-imported' });
  await expect(row).toBeVisible();
  await row.getByRole('button', { name: 'Edit account' }).click();
  await expect(page.locator('#user-edit-modal')).toHaveClass(/show/);
  await page.locator('#user-edit-form').getByRole('button', { name: 'Save changes' }).click();
  await expect(page.locator('#user-edit-modal')).not.toHaveClass(/show/);

  const refreshedRow = page.locator('#users-table-body tr', { hasText: 'browser-imported' });
  await refreshedRow.getByRole('button', { name: 'Issue code' }).click();
  await page.locator('#user-invitation-form').getByRole('button', { name: 'Generate code' }).click();
  await expect(page.locator('#user-invitation-code')).not.toHaveValue('');
  await expect(page.locator('#user-invitation-link')).toHaveAttribute('href', '/account/activate');
});
