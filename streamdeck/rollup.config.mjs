import typescript from "@rollup/plugin-typescript";
import nodeResolve from "@rollup/plugin-node-resolve";
import commonjs from "@rollup/plugin-commonjs";

export default {
  input: "src/plugin.ts",
  output: {
    file: "xyz.vaclavik.herdeck.sdPlugin/bin/plugin.js",
    format: "esm",
    sourcemap: true,
  },
  external: ["node:net", "node:child_process", "node:crypto", "node:os", "node:path", "node:stream", "node:events"],
  plugins: [
    // Override outDir to live under the bundle's bin/ (the Rollup `file` dir) and skip
    // .d.ts emit — the tsconfig outDir ("bin") is only meaningful for a bare `tsc` and
    // would otherwise conflict with the bundle path here.
    typescript({
      tsconfig: "./tsconfig.json",
      outDir: "xyz.vaclavik.herdeck.sdPlugin/bin",
      declaration: false,
      sourceMap: true,
    }),
    nodeResolve({ browser: false, preferBuiltins: true }),
    commonjs(),
  ],
};
