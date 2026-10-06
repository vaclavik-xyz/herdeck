import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { docs } from '../src/lib/docs';

test('without JavaScript, navigation and native film controls remain available', async ({
  browser,
}) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  await page.goto('http://127.0.0.1:4323/cs/');
  await expect(page.locator('.film-play')).toBeHidden();
  await expect(page.locator('video')).toHaveAttribute('controls', '');
  await expect(page.locator('herdeck-demo noscript')).toBeVisible();
  await page.locator('.language-switch').click();
  await expect(page).toHaveURL('/en/');
  await context.close();
});

for (const lang of ['en', 'cs'] as const) {
  test(`${lang}: the captured deck answers, pages and switches scenarios`, async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await page.goto(`/${lang}/`);
    await expect(page.locator('html')).toHaveAttribute('lang', lang);
    await expect(page.locator('herdeck-demo[data-ready]')).toBeAttached();
    const key = (i: number) => page.locator(`[data-key="${i}"]`);
    const cs = lang === 'cs';
    // The status window starts the triage loop on the agent waiting longest.
    await key(13).click();
    await expect(key(12)).toHaveAttribute('aria-label', cs ? 'Zpět' : 'Back');
    await expect(key(13)).toHaveAttribute('aria-label', /lumen/);
    await expect(key(0)).toHaveAttribute('aria-label', /^1 · /);
    // Answering moves straight on to the next blocked agent.
    await key(0).click();
    await expect(key(13)).toHaveAttribute('aria-label', /orbit/);
    await expect(key(0)).toHaveAttribute('aria-label', /^1 · /);
    await key(0).click();
    await expect(key(13)).toHaveAttribute('aria-label', cs ? /Vše v klidu/ : /All clear/, {
      timeout: 8000,
    });
    // A calm deck pages through the usage limits.
    await key(13).click();
    await expect(key(13)).toHaveAttribute('aria-label', cs ? /Limity využití/ : /Usage limits/);
    await page.locator('[data-scenario="offline"]').click();
    await expect(page.locator('[data-scenario="offline"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(key(13)).toHaveAttribute('aria-label', /Offline/i);
    await page.goto(`/${lang}/docs/connections/`);
    await page.locator('.language-switch').click();
    await expect(page).toHaveURL(`/${lang === 'cs' ? 'en' : 'cs'}/docs/connections/`);
    expect(errors).toEqual([]);
  });
  test(`${lang}: real film loads and plays only after a user click`, async ({ page }) => {
    const filmRequests: string[] = [];
    page.on('request', (r) => {
      if (r.url().endsWith('.mp4')) filmRequests.push(r.url());
    });
    await page.goto(`/${lang}/`);
    expect(await page.locator('video').evaluate((v: HTMLVideoElement) => v.paused)).toBe(true);
    expect(filmRequests).toEqual([]);
    // One play affordance: native controls stay hidden behind the overlay until playback.
    expect(await page.locator('video').evaluate((v: HTMLVideoElement) => v.controls)).toBe(false);
    await page.locator('.film-play').click();
    await expect
      .poll(() => page.locator('video').evaluate((v: HTMLVideoElement) => v.currentTime))
      .toBeGreaterThan(0.15);
    const state = await page.locator('video').evaluate((v: HTMLVideoElement) => ({
      duration: v.duration,
      muted: v.muted,
      error: v.error?.message,
      width: v.videoWidth,
      tracks: v.textTracks.length,
    }));
    expect(state.error).toBeUndefined();
    expect(state.duration).toBeGreaterThan(29);
    expect(state.duration).toBeLessThan(31);
    expect(state.muted).toBe(false);
    expect(state.width).toBe(1280);
    expect(state.tracks).toBe(1);
    await expect(page.locator('.film-play')).toBeHidden();
    expect(await page.locator('video').evaluate((v: HTMLVideoElement) => v.controls)).toBe(true);
  });
  test(`${lang}: routes, internal links, images and accessibility`, async ({ page, request }) => {
    const routes = [
      '',
      'features/',
      'workflow/',
      'download/',
      'credits/',
      'docs/',
      ...docs.map((d) => `docs/${d.slug}/`),
    ];
    const links = new Set<string>();
    for (const route of routes) {
      const response = await page.goto(`/${lang}/${route}`);
      expect(response?.status()).toBe(200);
      await expect(page.locator('h1')).toHaveCount(1);
      const broken = await page
        .locator('img')
        .evaluateAll((imgs) =>
          imgs
            .filter(
              (i): i is HTMLImageElement =>
                i instanceof HTMLImageElement && i.complete && i.naturalWidth === 0,
            )
            .map((i) => i.src),
        );
      expect(broken).toEqual([]);
      for (const href of await page
        .locator('a[href^="/"], img[src^="/"], source[src^="/"], track[src^="/"]')
        .evaluateAll((els) =>
          els.map((e) => e.getAttribute('href') || e.getAttribute('src') || ''),
        ))
        links.add(href.split('#')[0]);
      if (['', 'docs/getting-started/', 'download/'].includes(route)) {
        const result = await new AxeBuilder({ page })
          .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
          .analyze();
        expect(
          result.violations.map((v) => ({ id: v.id, nodes: v.nodes.map((n) => n.target) })),
        ).toEqual([]);
      }
    }
    for (const link of links) {
      const response = await request.head(link);
      expect(response.status(), link).toBe(200);
    }
  });
}

test('Czech docs search ignores accents and handles empty results', async ({ page }) => {
  await page.goto('/cs/docs/');
  const search = page.getByRole('searchbox');
  await search.fill('pripojeni');
  await expect(page.locator('[data-search][href="/cs/docs/connections/"]')).toBeVisible();
  await search.fill('unfindableqzx');
  await expect(page.locator('.search-empty')).toBeVisible();
  await search.fill('');
  await expect(page.locator('[data-search]:visible')).toHaveCount(docs.length);
});

test('mobile menu, keyboard controls, reduced motion and narrow layouts', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  for (const width of [360, 390, 768]) {
    await page.setViewportSize({ width, height: 844 });
    for (const route of [
      '/cs/',
      '/en/features/',
      '/cs/workflow/',
      '/en/download/',
      '/cs/docs/getting-started/',
    ]) {
      await page.goto(route);
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        `${route} at ${width}`,
      ).toBe(true);
    }
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/cs/');
  await page.getByRole('button', { name: 'Menu', exact: true }).click();
  await expect(page.locator('#mobile-nav')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('#mobile-nav')).toBeHidden();
  await expect(page.locator('herdeck-demo[data-ready]')).toBeAttached();
  await page.locator('[data-key="13"]').focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('[data-key="12"]')).toHaveAttribute('aria-label', 'Zpět');
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).scrollBehavior)).toBe(
    'auto',
  );
  const result = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
    .analyze();
  expect(result.violations.map((v) => v.id)).toEqual([]);
});

test('preview indexing policy, default language and real 404', async ({ page, request }) => {
  await page.goto('/');
  await expect(page).toHaveURL('/en/');
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute('content', 'noindex, nofollow');
  expect(await (await request.get('/robots.txt')).text()).toContain('Disallow: /');
  const response = await page.goto('/this-page-does-not-exist/');
  expect(response?.status()).toBe(404);
  await expect(page.getByRole('link', { name: 'Česká úvodní stránka' })).toBeVisible();
});
