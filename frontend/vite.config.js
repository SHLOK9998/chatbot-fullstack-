import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/auth': 'http://127.0.0.1:8000',
      '/chat': {
        target: 'http://127.0.0.1:8000',
        bypass(req) {
          // Only proxy actual API calls (POST/PUT/DELETE or paths with no extension)
          // Let GET requests to /chat/* that look like page navigations fall through to index.html
          if (req.method === 'GET' && !req.url.includes('/chat/') ) return req.url
        },
      },
    },
    historyApiFallback: true,
  },
})
