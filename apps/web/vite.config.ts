import { defineConfig } from "vite";

export default defineConfig({
  server: {
    host: "127.0.0.1",
    strictPort: true,
    proxy: {
      "/api": process.env.HYPNOREVIEW_API_TARGET || "http://127.0.0.1:8000",
    },
  },
});
