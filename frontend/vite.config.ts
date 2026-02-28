import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/config': 'http://69.62.93.146:8000',
      '/auto-generate': 'http://69.62.93.146:8000',
      '/generate-video': 'http://69.62.93.146:8000',
      '/health': 'http://69.62.93.146:8000',
      '/static': 'http://69.62.93.146:8000',
    }
  }
})
