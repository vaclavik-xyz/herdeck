import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, cpSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Do not let Remotion's bundler recursively copy previously rendered MP4s.
// Only the composition's small input assets belong in its public directory.
const assets = mkdtempSync(join(tmpdir(), 'herdeck-film-'));
try {
  for (const name of ['images', 'icons', 'fonts'])
    cpSync(`public/${name}`, join(assets, name), { recursive: true });
  cpSync('public/favicon.png', join(assets, 'favicon.png'));
  mkdirSync(join(assets, 'film'));
  cpSync('public/film/herdeck-score.mp3', join(assets, 'film/herdeck-score.mp3'));
  for (const lang of ['en', 'cs']) {
    const result = spawnSync(
      'npx',
      [
        '--no-install',
        'remotion',
        'render',
        'remotion/index.ts',
        `Herdeck-${lang}`,
        `public/film/herdeck-${lang}.mp4`,
        '--public-dir',
        assets,
        '--codec',
        'h264',
        '--crf',
        '21',
        '--concurrency',
        '2',
        '--log',
        'error',
      ],
      { stdio: 'inherit' },
    );
    if (result.status !== 0) process.exitCode = result.status || 1;
    if (process.exitCode) break;
  }
} finally {
  rmSync(assets, { recursive: true, force: true });
}
