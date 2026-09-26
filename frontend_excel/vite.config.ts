import fs from "node:fs";
import path from "node:path";
import { defineConfig } from "vite";

const certDir = path.resolve(__dirname, ".certs");
const keyFile = path.join(certDir, "localhost-key.pem");
const certFile = path.join(certDir, "localhost.pem");

const https =
  fs.existsSync(keyFile) && fs.existsSync(certFile)
    ? {
        key: fs.readFileSync(keyFile),
        cert: fs.readFileSync(certFile),
      }
    : undefined;

if (!https) {
  console.warn(
    "[vite] Missing .certs/localhost*.pem — run: brew install mkcert && mkcert -install && npm run certs",
  );
}

// Office Add-ins (Mac WebView) require a cert trusted by the system keychain.
// Proxy /api → FastAPI so the HTTPS task pane never calls plain http:// (mixed content).
export default defineConfig({
  server: {
    port: 3000,
    strictPort: true,
    https,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
