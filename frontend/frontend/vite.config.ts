import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const backend = process.env.VITE_BACKEND_URL || 'http://localhost:8080'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    allowedHosts: ['.monkeycode-ai.live'],
    proxy: {
      '/api': {
        target: backend,
        changeOrigin: true,
      },
    },
  },
})
