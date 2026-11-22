/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  /** @deprecated Use VITE_API_BASE_URL. */
  readonly VITE_API_BASE?: string;
  readonly VITE_API_PREFIX?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
