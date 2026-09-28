import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  use: {
    baseURL: 'http://127.0.0.1:4323',
    viewport: { width: 1440, height: 1000 },
    trace: 'retain-on-failure',
  },
  // Astro 7 backgrounds itself when launched by a coding agent. Playwright
  // owns this process, so keep it in the foreground with --ignore-lock.
  webServer: {
    command: 'npx astro preview --ignore-lock --host 127.0.0.1 --port 4323',
    url: 'http://127.0.0.1:4323/en/',
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
