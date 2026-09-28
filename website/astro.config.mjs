import { defineConfig } from 'astro/config';

// Set SITE_URL when a public domain is chosen. Previews intentionally stay noindex.
export default defineConfig({
  site: process.env.SITE_URL || undefined,
  output: 'static',
  trailingSlash: 'always',
  server: { host: '0.0.0.0', port: 4321 },
  devToolbar: { enabled: false },
});
