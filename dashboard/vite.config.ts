import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

const THEME_COLOR = "#0b0d10"; // anthracite, fond du dashboard (--color-canvas)

export default defineConfig({
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
        cleanupOutdatedCaches: true,
      },
    }),
  ],
  // `npm run dev -- --host` pour tester depuis un téléphone du réseau local
  server: { port: 5173, strictPort: true },
  preview: { port: 4173, strictPort: true },
});
