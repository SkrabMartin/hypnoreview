import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "../../tests/e2e",
  testMatch: "*.spec.ts",
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:15173",
    viewport: { width: 1440, height: 1000 },
  },
  webServer: [
    {
      command: `cd ../.. && ${process.platform === "win32" ? ".venv-signals\\Scripts\\python.exe" : ".venv-signals/bin/python"} tests/e2e/serve_reviewer.py`,
      url: "http://127.0.0.1:18000/api/v1/health",
      timeout: 60000,
      reuseExistingServer: false,
    },
    {
      command: "npm run dev -- --port 15173",
      url: "http://127.0.0.1:15173",
      env: { HYPNOREVIEW_API_TARGET: "http://127.0.0.1:18000" },
      reuseExistingServer: false,
    },
  ],
});
