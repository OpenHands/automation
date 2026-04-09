/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Core surface colors (dark theme)
        surface: {
          DEFAULT: "#050505",
          card: "#0a0a0a",
          elevated: "#1a1a1a",
        },
        // Border colors
        border: {
          DEFAULT: "#242424",
          hover: "#3a3a3a",
        },
        // Text colors
        content: {
          DEFAULT: "#fafafa",
          muted: "#8c8c8c",
          icon: "#3a3a3a",
        },
        // Toggle switch
        toggle: {
          active: "#34d399",
          "active-bg": "rgba(16, 185, 129, 0.2)",
          "active-border": "rgba(52, 211, 153, 0.5)",
          inactive: "#242424",
          "inactive-knob": "#8c8c8c",
          "inactive-border": "#3a3a3a",
        },
        // Legacy tokens (preserved from original config)
        modal: {
          background: "#171717",
          input: "#27272A",
          primary: "#F3CE49",
          secondary: "#737373",
          muted: "#A3A3A3",
        },
        org: {
          border: "#171717",
          background: "#262626",
          divider: "#525252",
          button: "#737373",
          text: "#A3A3A3",
        },
      },
    },
  },
  plugins: [],
};
