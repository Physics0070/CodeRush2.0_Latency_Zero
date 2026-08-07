import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // In dev the API runs separately; in the Docker image FastAPI serves this
    // build from the same origin, so no proxy is needed there.
    proxy: {
      '/health': 'http://localhost:7860',
      '/api': 'http://localhost:7860',
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
