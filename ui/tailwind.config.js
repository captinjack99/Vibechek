/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // Dark DJ-friendly surface palette
        surface: {
          50:  "#1e1e2e",  // lightest panels
          100: "#181825",
          200: "#11111b",  // base background
          300: "#0a0a13",  // deepest
        },
        accent: {
          DEFAULT: "#a855f7",  // purple
          cyan:    "#06b6d4",
          green:   "#10b981",
          yellow:  "#f59e0b",
          red:     "#ef4444",
        },
        // Energy gradient (1-5)
        energy: {
          1: "#10b981",
          2: "#84cc16",
          3: "#f59e0b",
          4: "#f97316",
          5: "#ef4444",
        },
      },
      fontFamily: {
        // Inter Variable is BUNDLED (main.tsx imports @fontsource-variable/
        // inter), so body + headings render identically on every OS instead
        // of the Segoe-UI / SF-Pro / DejaVu lottery. The system stack remains
        // as fallback for the pre-font-load frame.
        sans: [
          "Inter Variable", "Inter", "ui-sans-serif", "system-ui",
          "-apple-system", "Segoe UI", "Helvetica Neue", "Arial", "sans-serif",
        ],
        display: [
          "Inter Variable", "Inter", "ui-sans-serif", "system-ui", "sans-serif",
        ],
        // JetBrains Mono is bundled too — consistent metrics for the BPM /
        // key / size columns across platforms.
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "Consolas", "monospace"],
      },
      animation: {
        "fade-in": "fade-in 0.2s ease-out",
        "slide-up": "slide-up 0.3s ease-out",
      },
      keyframes: {
        "fade-in": {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        "slide-up": {
          "0%": { opacity: "0", transform: "translateY(10px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
    },
  },
  plugins: [],
};
