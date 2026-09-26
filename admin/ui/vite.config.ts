import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'
import { VitePWA } from 'vite-plugin-pwa'
import { pwaOptions } from './pwa.ts'

export default defineConfig({
  plugins: [vue(), tailwindcss(), VitePWA(pwaOptions(import.meta.dirname))],
  resolve: { alias: { '@': path.resolve(import.meta.dirname, './src') } },
  server: { host: '0.0.0.0', port: 5173, strictPort: true },
})
