/// <reference types="vite/client" />

interface ImportMetaEnv {
  // Optional — defaults to "/substrate" (same-origin proxy). Override only
  // when pointing the dev build at a non-default proxy path.
  readonly VITE_SUBSTRATE_PROXY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
