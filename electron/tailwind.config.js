/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./src/renderer/**/*.{html,ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: 'var(--bg)',
        surface: 'var(--surface)',
        'surface-2': 'var(--surface-2)',
        'surface-3': 'var(--surface-3)',
        line: 'var(--line)',
        'line-2': 'var(--line-2)',
        ink: {
          DEFAULT: 'var(--ink)',
          50: '#f5f5f7',
          100: '#eef0f6',
          200: '#e5e5ea',
          300: '#d6d6db',
          400: '#a1a1a6',
          500: '#6e6e73',
          600: '#3a3a3e',
          700: '#363c52',
          800: '#20263a',
          900: '#1d1d1f',
          950: '#080b12',
          1: 'var(--ink)',
          2: 'var(--ink-2)',
          3: 'var(--ink-3)',
          4: 'var(--ink-4)'
        },
        accent: {
          DEFAULT: 'var(--accent)',
          400: '#409cff',
          500: '#007aff',
          600: '#0066d6',
          tint: 'var(--accent-tint)'
        },
        success: '#34c759',
        warn: '#ff9500',
        risky: {
          50: '#fff1f2',
          500: '#ff3b30',
          700: '#b91c1c'
        }
      },
      fontFamily: {
        sans: [
          'Inter',
          'Geist',
          '-apple-system',
          'BlinkMacSystemFont',
          'SF Pro Text',
          'SF Pro Display',
          'system-ui',
          'sans-serif'
        ],
        mono: ['Roboto Mono', 'Geist Mono', 'ui-monospace', 'SF Mono', 'monospace']
      },
      boxShadow: {
        sm: '0 1px 2px rgba(0,0,0,0.04), 0 0 0 0.5px rgba(0,0,0,0.05)',
        md: '0 4px 16px rgba(0,0,0,0.06), 0 0 0 0.5px rgba(0,0,0,0.06)',
        lg: '0 12px 40px rgba(0,0,0,0.08), 0 0 0 0.5px rgba(0,0,0,0.06)',
        soft: '0 1px 0 rgba(16, 19, 30, 0.04), 0 8px 24px -16px rgba(16, 19, 30, 0.18)',
        accent: '0 1px 2px rgba(0,122,255,0.4)'
      },
      borderRadius: {
        DEFAULT: '10px',
        lg: '14px'
      },
      keyframes: {
        'fade-in-up': {
          '0%': { opacity: '0', transform: 'translateY(4px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        }
      },
      animation: {
        'fade-in-up': 'fade-in-up 250ms cubic-bezier(0.16, 1, 0.3, 1)',
        'fade-in': 'fade-in 150ms ease-out',
      }
    }
  },
  plugins: []
}
