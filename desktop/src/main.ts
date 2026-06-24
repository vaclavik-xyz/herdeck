import { mount } from "svelte";
import App from "./App.svelte";

// Svelte 5 mount API. The target div lives in index.html.
const app = mount(App, {
  target: document.getElementById("app")!,
});

export default app;
