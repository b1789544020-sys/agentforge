import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import en from "./locales/en.json";
import zh from "./locales/zh.json";

const STORAGE_KEY = "agentforge.lang";

function detectInitialLanguage(): string {
  // 1. Honour an explicit user choice from a previous session.
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "en" || saved === "zh") return saved;
  } catch {
    // localStorage may be disabled (e.g. private mode); fall through.
  }

  // 2. Fall back to the browser language.  Treat any Chinese locale as
  //    Chinese, otherwise default to English.
  const nav = typeof navigator !== "undefined" ? navigator.language : "en";
  return nav.toLowerCase().startsWith("zh") ? "zh" : "en";
}

void i18n.use(initReactI18next).init({
  resources: {
    en: { translation: en },
    zh: { translation: zh },
  },
  lng: detectInitialLanguage(),
  fallbackLng: "en",
  interpolation: { escapeValue: false }, // React already escapes values
});

export function setLanguage(lang: "en" | "zh"): void {
  void i18n.changeLanguage(lang);
  try {
    localStorage.setItem(STORAGE_KEY, lang);
  } catch {
    // ignore
  }
  // Keep the <html lang> attribute in sync so screen readers and
  // browser translation prompts work correctly.
  if (typeof document !== "undefined") {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }
}

export function getCurrentLanguage(): "en" | "zh" {
  return (i18n.language as "en" | "zh") ?? "en";
}

export default i18n;
