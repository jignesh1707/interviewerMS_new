import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const backend = process.env.VITE_BACKEND_URL || 'http://localhost:8080'
// Dev server listens on loopback only; set VITE_HOST=0.0.0.0 and VITE_ALLOWED_HOSTS=host1,host2 to expose it.
const allowedHosts = (process.env.VITE_ALLOWED_HOSTS || '').split(',').map((h) => h.trim()).filter(Boolean)

export default defineConfig({
  plugins: [react()],
  server: {
    host: process.env.VITE_HOST || '127.0.0.1',
    port: 5173,
    allowedHosts,
    proxy: {
      '/api': {
        target: backend,
        changeOrigin: true,
      },
    },
  },
})
