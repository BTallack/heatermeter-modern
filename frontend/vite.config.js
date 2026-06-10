import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'
import tailwindcss from '@tailwindcss/vite'

// base: './' so the build works at any mount path (/app during build-out, / at
// cutover). All assets are bundled locally — no CDN, fully offline at runtime.
export default defineConfig({
  plugins: [tailwindcss(), svelte()],
  base: './',
  build: { outDir: 'dist', emptyOutDir: true },
})
