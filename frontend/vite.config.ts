import react from '@vitejs/plugin-react'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  server: {
    proxy: {
      '/api':
        loadEnv(mode, '.', '').API_PROXY_TARGET || 'http://127.0.0.1:8000',
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    maxWorkers: 2,
    // user-event flows take <0.5s idle but exceed 5s when the machine is busy.
    testTimeout: 15000,
  },
}))
