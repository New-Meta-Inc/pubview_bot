/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/**/*.{astro,html,js,jsx,ts,tsx,md,mdx,svelte,vue}'],
  theme: {
    extend: {
      colors: {
        // 寮テーマカラー（LoL Elemental Drake 準拠、内部ID は旧名のまま）
        raptor: { DEFAULT: '#3FA34D', dark: '#1F5A2A' }, // → ケミテック (緑)
        krug:   { DEFAULT: '#2E7BB8', dark: '#143C5A' }, // → オーシャン (青)
        wolf:   { DEFAULT: '#DC4438', dark: '#6B201A' }, // → インファーナル (赤)
        gromp:  { DEFAULT: '#7A5FA8', dark: '#46356A' }, // → ヘクステック (紫、既存維持)
      },
    },
  },
  plugins: [],
};
