import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import ui from "@nuxt/ui/vite";
import { resolve } from "node:path";
export default defineConfig({
  plugins: [
    vue(),
    ui({
      router: false,
      colorMode: false,
      dts: false,
      ui: { colors: { primary: "blue", neutral: "slate" } },
      icon: { clientBundle: { scan: true } },
    }),
  ],
  build: {
    manifest: true,
    target: "es2022",
    rollupOptions: {
      input: Object.fromEntries(
        [
          "index",
          "cases",
          "news",
          "skills",
          "services",
          "deploy",
          "profile",
          "detail",
          "officers",
          "login",
        ].map((name) => [name, resolve(import.meta.dirname, `${name}.html`)]),
      ),
    },
  },
});
