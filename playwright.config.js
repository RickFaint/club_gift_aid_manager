const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests/browser',
  fullyParallel: false,
  timeout: 30_000,
  expect: { timeout: 5_000 },
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: 'http://127.0.0.1:5173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{
    name: 'chromium',
    use: {
      ...devices['Desktop Chrome'],
      // Developers can use an existing Chrome; CI installs bundled Chromium.
      ...(process.env.CI ? {} : { channel: 'chrome' }),
    },
  }],
  webServer: {
    command: 'python tests/browser/server.py',
    url: 'http://127.0.0.1:5173/login',
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
