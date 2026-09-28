import type { APIRoute } from 'astro';
import { languages, path } from '../lib/copy';
import { docs } from '../lib/docs';
export const GET: APIRoute = ({ site }) => {
  const pages = [
    '',
    'features',
    'workflow',
    'download',
    'credits',
    'docs',
    ...docs.map((d) => `docs/${d.slug}`),
  ];
  const urls = site
    ? languages
        .flatMap((lang) =>
          pages.map((page) => `<url><loc>${new URL(path(lang, page), site)}</loc></url>`),
        )
        .join('')
    : '';
  return new Response(
    `<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${urls}</urlset>`,
    { headers: { 'Content-Type': 'application/xml' } },
  );
};
