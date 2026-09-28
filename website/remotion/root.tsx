import React, { useEffect, useState } from 'react';
import {
  AbsoluteFill,
  Composition,
  Html5Audio,
  Img,
  Sequence,
  continueRender,
  delayRender,
  interpolate,
  staticFile,
  useCurrentFrame,
} from 'remotion';

// 64 beats at 128 BPM = 30s = 900 frames. Cuts are locked to musical phrases.
const FPS = 30;
const beat = (n: number) => Math.round(((n * 60) / 128) * FPS);
type Props = { lang: 'en' | 'cs' };
const navy = '#09111f',
  cyan = '#78dfed',
  white = '#edf3fa',
  amber = '#edb66b';
const font = 'Sora, sans-serif';
const copy = {
  en: {
    intro: ['Your agents.', 'At your fingertips.'],
    busy: ['Claude is waiting.', 'Codex is building.'],
    fleet: ['One fleet.', 'One view.'],
    read: 'Read the request.',
    call: 'Make the call.',
    flow: 'Back in flow.',
    surfaces: ['On your deck.', 'On your desktop.', 'Across your machines.'],
    end: 'Your agents. Under your control.',
    prompt: 'Run the project test suite?',
    approve: 'Approve',
    denied: 'Deny',
    working: 'Working',
    blocked: 'Needs you',
    done: 'Done',
  },
  cs: {
    intro: ['Tvoji agenti.', 'Na dosah ruky.'],
    busy: ['Claude čeká.', 'Codex pracuje.'],
    fleet: ['Jedna flotila.', 'Jeden přehled.'],
    read: 'Přečti požadavek.',
    call: 'Rozhodni.',
    flow: 'A pokračuj.',
    surfaces: ['Na decku.', 'Na desktopu.', 'Napříč stroji.'],
    end: 'Tvoji agenti pod tvou kontrolou.',
    prompt: 'Spustit testy projektu?',
    approve: 'Schválit',
    denied: 'Zamítnout',
    working: 'Pracuje',
    blocked: 'Čeká na tebe',
    done: 'Hotovo',
  },
};

function Scene({
  children,
  photo,
  align = 'left',
}: {
  children: React.ReactNode;
  photo?: string;
  align?: 'left' | 'center';
}) {
  const f = useCurrentFrame();
  const entrance = interpolate(f, [0, 12], [0, 1], { extrapolateRight: 'clamp' });
  return (
    <AbsoluteFill style={{ background: navy, overflow: 'hidden', fontFamily: font, color: white }}>
      {photo && (
        <>
          <Img
            src={staticFile(`images/${photo}.webp`)}
            style={{
              width: '100%',
              height: '100%',
              objectFit: 'cover',
              transform: `scale(${1 + f * 0.00035})`,
            }}
          />
          <AbsoluteFill
            style={{ background: 'linear-gradient(90deg,#09111ff5,#09111fb0 40%,#09111f20)' }}
          />
        </>
      )}
      <AbsoluteFill
        style={{
          padding: '80px',
          justifyContent: 'center',
          alignItems: align === 'center' ? 'center' : 'flex-start',
          textAlign: align,
          opacity: entrance,
          transform: `translateY(${(1 - entrance) * 22}px)`,
        }}
      >
        {children}
      </AbsoluteFill>
      <div
        style={{
          position: 'absolute',
          bottom: 30,
          left: 40,
          right: 40,
          display: 'flex',
          justifyContent: 'space-between',
          fontSize: 14,
          color: '#acbdd1',
        }}
      >
        <span>herdeck</span>
        <span>
          ● <span style={{ color: cyan }}>●</span> ●
        </span>
      </div>
    </AbsoluteFill>
  );
}
function Title({ children, size = 82 }: { children: React.ReactNode; size?: number }) {
  return (
    <div
      style={{
        fontSize: size,
        fontWeight: 550,
        letterSpacing: '-.055em',
        lineHeight: 1.12,
        whiteSpace: 'pre-line',
        maxWidth: 1080,
      }}
    >
      {children}
    </div>
  );
}
function Tiles({ lang, resolved = false }: { lang: Props['lang']; resolved?: boolean }) {
  const f = useCurrentFrame();
  const t = copy[lang];
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(4, 190px)',
        gap: 16,
        transform: `translateY(${interpolate(f, [0, 15], [30, 0], { extrapolateRight: 'clamp' })}px)`,
      }}
    >
      {['claude', 'codex', 'gemini', 'cursor', 'hermes', 'opencode', 'claude', 'codex'].map(
        (a, i) => {
          const color = i === 0 && !resolved ? amber : i === 2 ? cyan : '#81d7ad';
          return (
            <div
              key={i}
              style={{
                border: `1px solid ${i === 0 ? color : '#35475f'}`,
                borderRadius: 12,
                background: i === 0 && !resolved ? '#372a20' : '#142133',
                height: 155,
                padding: 20,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexDirection: 'column',
                gap: 12,
                position: 'relative',
                overflow: 'hidden',
              }}
            >
              <Img src={staticFile(`icons/${a}.svg`)} style={{ width: 36, height: 36 }} />
              <span style={{ fontSize: 16 }}>
                {
                  [
                    'herdeck',
                    'api-server',
                    'docs',
                    'web-app',
                    'pipeline',
                    'design',
                    'auth',
                    'checkout',
                  ][i]
                }
              </span>
              <span style={{ fontSize: 12, color }}>
                {i === 0 && !resolved ? t.blocked : i === 2 ? t.done : t.working}
              </span>
              <div
                style={{
                  height: 4,
                  background: color,
                  position: 'absolute',
                  bottom: 0,
                  left: 0,
                  right: 0,
                }}
              />
            </div>
          );
        },
      )}
    </div>
  );
}
function Approval({ lang }: { lang: Props['lang'] }) {
  const f = useCurrentFrame();
  const t = copy[lang];
  const approved = f >= beat(4);
  return (
    <Scene>
      <Title size={70}>{approved ? t.call : t.read}</Title>
      <div
        style={{
          background: '#142236',
          border: '1px solid #536477',
          borderRadius: 14,
          padding: 35,
          marginTop: 40,
          width: 720,
          boxShadow: '0 30px 70px #0006',
        }}
      >
        <div style={{ fontSize: 18, color: approved ? cyan : amber, marginBottom: 18 }}>
          Claude · herdeck
        </div>
        <div style={{ fontSize: 30, marginBottom: 22 }}>{t.prompt}</div>
        <div
          style={{
            fontFamily: 'monospace',
            fontSize: 22,
            padding: 20,
            background: '#08121f',
            borderRadius: 6,
          }}
        >
          npm test
        </div>
        <div
          style={{
            display: 'flex',
            justifyContent: 'flex-end',
            gap: 18,
            marginTop: 24,
            fontSize: 20,
          }}
        >
          <span style={{ padding: '12px 22px', border: '1px solid #51627a', borderRadius: 6 }}>
            {t.denied}
          </span>
          <span
            style={{
              padding: '12px 24px',
              background: approved ? '#81d7ad' : cyan,
              color: navy,
              borderRadius: 6,
              transform: approved ? 'scale(.96)' : 'none',
            }}
          >
            {approved ? '✓ ' : ''}
            {t.approve}
          </span>
        </div>
      </div>
    </Scene>
  );
}
export function Film({ lang }: Props) {
  const t = copy[lang];
  const [handle] = useState(() => delayRender('Loading local film fonts'));
  useEffect(() => {
    let cancelled = false;
    Promise.all([
      new FontFace('Sora', `url(${staticFile('fonts/sora-latin.woff2')})`, {
        weight: '100 800',
        unicodeRange: 'U+0000-00FF',
      }).load(),
      new FontFace('Sora', `url(${staticFile('fonts/sora-latin-ext.woff2')})`, {
        weight: '100 800',
        unicodeRange: 'U+0100-024F,U+1E00-1EFF,U+2000-206F',
      }).load(),
    ])
      .then((fonts) => {
        if (!cancelled) {
          fonts.forEach((f) => document.fonts.add(f));
          continueRender(handle);
        }
      })
      .catch((e) => {
        throw e;
      });
    return () => {
      cancelled = true;
    };
  }, [handle]);
  return (
    <AbsoluteFill style={{ background: navy }}>
      <Html5Audio src={staticFile('film/herdeck-score.mp3')} />
      <Sequence from={0} durationInFrames={beat(8)}>
        <Scene photo="deck-hero">
          <Title>{t.intro.join('\n')}</Title>
          <div style={{ fontSize: 20, marginTop: 28, color: cyan }}>herdeck</div>
        </Scene>
      </Sequence>
      <Sequence from={beat(8)} durationInFrames={beat(8)}>
        <Scene>
          <Title size={66}>{t.busy.join('\n')}</Title>
          <div style={{ marginTop: 35, transform: 'scale(.82)', transformOrigin: 'top left' }}>
            <Tiles lang={lang} />
          </div>
        </Scene>
      </Sequence>
      <Sequence from={beat(16)} durationInFrames={beat(8)}>
        <Scene align="center">
          <Title size={65}>{t.fleet.join(' ')}</Title>
          <div style={{ marginTop: 42 }}>
            <Tiles lang={lang} />
          </div>
        </Scene>
      </Sequence>
      <Sequence from={beat(24)} durationInFrames={beat(8)}>
        <Approval lang={lang} />
      </Sequence>
      <Sequence from={beat(32)} durationInFrames={beat(8)}>
        <Scene photo="deck-detail">
          <Title size={100}>{t.flow}</Title>
          <div style={{ marginTop: 28, fontSize: 20, color: cyan }}>✓ {t.working}</div>
        </Scene>
      </Sequence>
      {t.surfaces.map((line, i) => (
        <Sequence key={line} from={beat(40 + i * 4)} durationInFrames={beat(4)}>
          <Scene photo={i === 0 ? 'deck-hero' : undefined} align={i === 0 ? 'left' : 'center'}>
            <Title size={76}>{line}</Title>
            {i === 1 && (
              <div
                style={{ marginTop: 40, transform: 'scale(.72)', transformOrigin: 'top center' }}
              >
                <Tiles lang={lang} resolved />
              </div>
            )}
            {i === 2 && (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 38,
                  marginTop: 55,
                  color: cyan,
                  fontSize: 24,
                }}
              >
                <span>Mac</span>
                <span>────────</span>
                <Img src={staticFile('favicon.png')} style={{ width: 75, height: 75 }} />
                <span>────────</span>
                <span>Server</span>
              </div>
            )}
          </Scene>
        </Sequence>
      ))}
      <Sequence from={beat(52)} durationInFrames={900 - beat(52)}>
        <Scene align="center">
          <Img
            src={staticFile('favicon.png')}
            style={{ width: 115, height: 115, marginBottom: 26, borderRadius: 26 }}
          />
          <Title size={106}>herdeck</Title>
          <div style={{ fontSize: 26, marginTop: 25, color: '#c4d5e7' }}>{t.end}</div>
          <div style={{ fontSize: 16, marginTop: 30, color: cyan }}>
            github.com/vaclavik-xyz/herdeck
          </div>
        </Scene>
      </Sequence>
    </AbsoluteFill>
  );
}
export const Root = () => (
  <>
    {(['en', 'cs'] as const).map((lang) => (
      <Composition
        key={lang}
        id={`Herdeck-${lang}`}
        component={Film}
        durationInFrames={900}
        fps={FPS}
        width={1280}
        height={720}
        defaultProps={{ lang }}
      />
    ))}
  </>
);
