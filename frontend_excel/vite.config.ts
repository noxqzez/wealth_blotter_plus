import { defineConfig } from "vite";
import mkcert from "vite-plugin-mkcert";

// Office Add-ins require HTTPS even for local development.
export default defineConfig({
  plugins: [mkcert()],
  server: {
    port: 3000,
    strictPort: true,
    https: true,
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
