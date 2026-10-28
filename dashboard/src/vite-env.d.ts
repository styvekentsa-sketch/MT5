/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** URL de l'API du bot utilisée par défaut (modifiable dans les paramètres de l'application) */
  readonly VITE_API_BASE_URL?: string;
  /** Ancien nom de VITE_API_BASE_URL, toujours accepté */
  readonly VITE_API_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
