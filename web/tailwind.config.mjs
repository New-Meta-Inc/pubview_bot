/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/**/*.{astro,html,js,jsx,ts,tsx,md,mdx,svelte,vue}'],
  theme: {
    extend: {
      colors: {
        // 寮テーマカラー（LoLジャングルキャンプ準拠）
        raptor: { DEFAULT: '#3FA34D', dark: '#1F5A2A' },
        krug:   { DEFAULT: '#A0522D', dark: '#5B2E18' },
        wolf:   { DEFAULT: '#5B7C99', dark: '#324A60' },
        gromp:  { DEFAULT: '#7A5FA8', dark: '#46356A' },
      },
    },
  },
  plugins: [],
};
