/// <reference types="svelte" />
/// <reference types="vite/client" />

// Injected by vite.config.ts from package.json's version — see the note there.
declare const __APP_VERSION__: string;

// The xterm.js bundle vendored for the browser dashboard, reused by the agent
// card's live terminal (see lib/xtermLoader.ts and the alias in vite.config.ts).
declare module "@herdeck-web/*";
