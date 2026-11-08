import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";
import { defineConfig, loadEnv, type ProxyOptions } from "vite";
import { VitePWA } from "vite-plugin-pwa";

const THEME_COLOR = "#0b0d10"; // anthracite, fond du dashboard (--color-canvas)

export default defineConfig(({ mode }) => {
  // DASHBOARD_PORT (dashboard/.env) : port dédié du dashboard, 5173 par défaut. strictPort : jamais
  // de bascule silencieuse vers un autre port (l'API n'autorise que les origines déclarées).
  const env = loadEnv(mode, process.cwd(), "");
  const port = Number(env.DASHBOARD_PORT) || 5173;
  // Relais vers l'API du bot (API_PORT, dashboard/.env) : la page appelle sa propre adresse, ce qui
  // fonctionne aussi à distance (Tailscale) sans exposer le port de l'API.
  const api = `http://127.0.0.1:${Number(env.API_PORT) || 8000}`;
  // DASHBOARD_AUTO_VIEWER=true : une requête SANS clé reçoit la clé en lecture seule (API_VIEWER_KEY du
  // .env du bot). Le serveur n'écoute que sur 127.0.0.1 : seuls ce PC et les appareils invités sur
  // Tailscale l'atteignent, et ils consultent le bot en un clic. La clé ne quitte jamais ce PC ; toute
  // action exige toujours la clé complète (API_KEY).
  const viewerKey =
    env.DASHBOARD_AUTO_VIEWER === "true" ? (loadEnv(mode, resolve(process.cwd(), ".."), "API_VIEWER_KEY").API_VIEWER_KEY ?? "") : "";
  const toApi: ProxyOptions = {
    target: api,
    configure: (server) => {
      if (!viewerKey) return;
      server.on("proxyReq", (proxyReq, req) => {
        if (!req.headers["x-api-key"]) proxyReq.setHeader("X-API-Key", viewerKey);
      });
    },
  };
  const proxy = { "/api": toApi, "/health": toApi };
  return {
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      registerType: "autoUpdate",
      injectRegister: "auto",
      includeAssets: ["favicon.ico", "apple-touch-icon-180x180.png", "logo.svg"],
      manifest: {
        id: "/",
        name: "MT5 Trading Bot — Dashboard",
        short_name: "MT5 Bot",
        description: "Supervision et contrôle du bot de trading MetaTrader 5 : statut, compte, positions, sentiment.",
        lang: "fr",
        start_url: "/",
        scope: "/",
        display: "standalone",
        display_override: ["standalone", "minimal-ui"],
        orientation: "any",       // portrait et paysage : téléphones, tablettes, écrans de bureau
        theme_color: THEME_COLOR,
        background_color: THEME_COLOR,
        categories: ["finance", "productivity"],
        icons: [
          { src: "pwa-64x64.png", sizes: "64x64", type: "image/png" },
          { src: "pwa-192x192.png", sizes: "192x192", type: "image/png" },
          { src: "pwa-512x512.png", sizes: "512x512", type: "image/png" },
          { src: "maskable-icon-512x512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
        ],
      },
      workbox: {
        // Seule l'interface est mise en cache : les données de trading (autre origine,
        // l'API du bot) ne le sont jamais, pour ne pas afficher un état périmé.
        globPatterns: ["**/*.{js,css,html,ico,png,svg,woff2}"],
        navigateFallback: "/index.html",
        navigateFallbackDenylist: [/^\/api\//, /^\/health/],
        cleanupOutdatedCaches: true,
      },
    }),
  ],
  // `npm run dev -- --host` pour tester depuis un téléphone du réseau local
  // 127.0.0.1 : jamais exposé directement au réseau ; accès distant par `tailscale serve` (HTTPS, ts.net)
  server: { host: "127.0.0.1", port, strictPort: true, proxy, allowedHosts: [".ts.net"] },
  preview: { host: "127.0.0.1", port: 4173, strictPort: true, proxy, allowedHosts: [".ts.net"] },
  };
});
