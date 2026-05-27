import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    /** 不与 digital twin（5173）冲突 */
    port: 5174,
    proxy: {
      '/api': 'http://127.0.0.1:8001',
    },
  },
})
