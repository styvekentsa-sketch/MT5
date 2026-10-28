import { defineConfig, minimal2023Preset } from "@vite-pwa/assets-generator/config";

// Génère les icônes PWA depuis public/logo.svg : `npm run generate-pwa-assets`
const DARK_BACKGROUND = "#020617";

export default defineConfig({
  headLinkOptions: { preset: "2023" },
  preset: {
    ...minimal2023Preset,
    maskable: { ...minimal2023Preset.maskable, resizeOptions: { background: DARK_BACKGROUND } },
    apple: { ...minimal2023Preset.apple, resizeOptions: { background: DARK_BACKGROUND } },
  },
  images: ["public/logo.svg"],
});
