/// <reference types="vite/client" />

import type { BridgeApi } from '../../shared/bridge'

declare global {
  interface Window {
    inkforge: BridgeApi
  }
}

export {}
