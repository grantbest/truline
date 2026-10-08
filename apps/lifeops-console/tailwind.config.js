/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: {
          DEFAULT: "#0b0d10",
          panel: "#11141a",
          subtle: "#161a22",
          hover: "#1c2230",
        },
        fg: {
          DEFAULT: "#e6e8eb",
          muted: "#8a93a3",
          subtle: "#5d6573",
        },
        border: {
          DEFAULT: "#222833",
          strong: "#2e3645",
        },
        accent: {
          DEFAULT: "#7cc4ff",
          muted: "#3a6a96",
        },
        pos: "#3ddc84",
        neg: "#ff6b6b",
        warn: "#ffb454",
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Monaco", "Consolas", "monospace"],
        sans: ["Inter", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
      },
      fontSize: {
        "2xs": "0.6875rem",
      },
      keyframes: {
        "fade-in": { from: { opacity: "0" }, to: { opacity: "1" } },
        "slide-in": { from: { transform: "translateX(100%)" }, to: { transform: "translateX(0)" } },
        "slide-up": { from: { transform: "translateY(100%)" }, to: { transform: "translateY(0)" } },
      },
      animation: {
        "fade-in": "fade-in 150ms ease-out",
        "slide-in": "slide-in 200ms ease-out",
        "slide-up": "slide-up 200ms ease-out",
      },
    },
  },
  plugins: [],
};
