import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In the cluster, the Ingress sends /api and /mcp to the gateway and everything
// else to this app. `npm run dev` reproduces that split with a proxy, pointing
// at the gateway you already have running (the local cluster by default).
const gateway = process.env.GATEWAY_URL ?? 'http://gateway.localtest.me'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: gateway, changeOrigin: true },
      '/mcp': { target: gateway, changeOrigin: true },
    },
  },
})
