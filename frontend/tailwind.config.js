/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          50:  '#eff6ff',
          100: '#dbeafe',
          400: '#06B6D4',
          500: '#2563EB',
          600: '#1d4ed8',
          700: '#1e40af',
        },
        accent: {
          400: '#06B6D4',
          500: '#0891b2',
        },
      },
    },
  },
  plugins: [],
}
