import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'
import { vendorChunks } from './build/vendorChunks.js'
import { pdfAssets } from './build/pdfAssets.js'

export default defineConfig({
  plugins: [vue(), pdfAssets()],
  build: {
    rollupOptions: {
      output: {
        onlyExplicitManualChunks: true,
        manualChunks: vendorChunks(),
      },
    },
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  server: {
    port: 5174,
    strictPort: true,
    proxy: {
      '/api': {
        target: process.env.EIC_DEV_API_URL || 'http://127.0.0.1:8080',
        changeOrigin: true
      }
    }
  }
})
