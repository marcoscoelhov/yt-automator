import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const proxyTarget = env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8020'

  return {
    plugins: [react()],
    server: {
      proxy: {
        '/config': { target: proxyTarget, changeOrigin: true },
        '/auto-generate': { target: proxyTarget, changeOrigin: true },
        '/generate-video': { target: proxyTarget, changeOrigin: true },
        '/health': { target: proxyTarget, changeOrigin: true },
        '/static': { target: proxyTarget, changeOrigin: true },
      }
    }
  }
})
