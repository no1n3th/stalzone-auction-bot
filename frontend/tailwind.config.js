/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Редизайн 4.3 — единые токены; имена сохранены для совместимости
        base: "#0d0f12",
        surface: "#13161a",
        "surface-2": "#191d22",
        line: "#262b31",
        ink: "#e6e8eb",
        muted: "#a0a8b0",
        subtle: "#7c858e",
        accent: "#6fa3d8",
        gold: { DEFAULT: "#6fa3d8", hi: "#8cb8e4" },
        danger: "#e5646b",
        profit: "#5dbb8a",
        warn: "#d8a657",
      },
      fontFamily: {
        sans: ['"IBM Plex Sans"', "system-ui", "-apple-system", "sans-serif"],
        mono: ['"IBM Plex Mono"', "ui-monospace", "monospace"],
      },
      transitionTimingFunction: {
        smooth: "cubic-bezier(0.22, 1, 0.36, 1)",
      },
    },
  },
  plugins: [],
};
