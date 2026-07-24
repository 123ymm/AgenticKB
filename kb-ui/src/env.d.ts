/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_CONTROL_PLANE_API_BASE: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
