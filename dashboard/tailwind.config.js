/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./src/**/*.{js,jsx,ts,tsx}",
    "./public/index.html"
  ],
  theme: {
    extend: {
      colors: {
        primary: {
          50: '#eff6ff',
          100: '#dbeafe',
          200: '#bfdbfe',
          300: '#93c5fd',
          400: '#60a5fa',
          500: '#3b82f6',
          600: '#2563eb',
          700: '#1d4ed8',
          800: '#1e40af',
          900: '#1e3a8a',
        },
        severity: {
          blocker: '#dc2626',
          critical: '#ea580c',
          major: '#f59e0b',
          minor: '#84cc16',
          info: '#06b6d4',
        },
        quality: {
          A: '#22c55e',
          B: '#84cc16',
          C: '#f59e0b',
          D: '#ea580c',
          E: '#dc2626',
        }
      }
    },
  },
  plugins: [],
}
