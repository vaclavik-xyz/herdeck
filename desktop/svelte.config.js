import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

// vitePreprocess lets <script lang="ts"> work in .svelte files (type-stripping).
export default {
  preprocess: vitePreprocess(),
};
