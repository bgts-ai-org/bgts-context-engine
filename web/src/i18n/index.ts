/**
 * Minimal i18n layer: two static catalogs, `{name}` placeholder substitution and a React
 * context so the locale can be swapped without a reload. English is the default.
 *
 * Deliberately dependency-free. The catalogs are small and fully typed, which buys key
 * completeness at compile time - something a runtime-loading library would not give us.
 */
import { createContext, useContext } from "react";

import { en, type TranslationKey } from "./en";
import { tr } from "./tr";

export type { TranslationKey };

export const LOCALES = ["en", "tr"] as const;
export type Locale = (typeof LOCALES)[number];

export const DEFAULT_LOCALE: Locale = "en";

const CATALOGS: Record<Locale, Record<TranslationKey, string>> = { en, tr };

/** Names shown in the language picker, each in its own language. */
export const LOCALE_NAMES: Record<Locale, string> = { en: "English", tr: "Türkçe" };

const STORAGE_KEY = "bce.locale";

export function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && (LOCALES as readonly string[]).includes(value);
}

export type TranslateVars = Record<string, string | number>;

export function translate(locale: Locale, key: TranslationKey, vars?: TranslateVars): string {
  const template = CATALOGS[locale][key] ?? en[key];
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in vars ? String(vars[name]) : match,
  );
}

export type TranslateFn = (key: TranslationKey, vars?: TranslateVars) => string;

/**
 * Resolve the initial locale, most explicit signal first: a `?lang=` query parameter, a
 * previously stored choice, then the browser's preference. `backendDefault` comes from
 * BCE_DEFAULT_LOCALE via /v1/ui/config and acts as the deployment-wide default.
 */
export function resolveInitialLocale(backendDefault?: string): Locale {
  const fromQuery = new URLSearchParams(window.location.search).get("lang");
  if (isLocale(fromQuery)) return fromQuery;

  const stored = readStoredLocale();
  if (stored) return stored;

  if (isLocale(backendDefault)) return backendDefault;

  const fromBrowser = navigator.language?.split("-")[0];
  if (isLocale(fromBrowser)) return fromBrowser;

  return DEFAULT_LOCALE;
}

export function readStoredLocale(): Locale | null {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return isLocale(stored) ? stored : null;
  } catch {
    // Storage can be unavailable (private mode, disabled cookies); fall back to defaults.
    return null;
  }
}

export function storeLocale(locale: Locale): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, locale);
  } catch {
    // Persisting the preference is best-effort; the session still works without it.
  }
}

interface I18nValue {
  locale: Locale;
  t: TranslateFn;
  setLocale: (locale: Locale) => void;
}

export const I18nContext = createContext<I18nValue>({
  locale: DEFAULT_LOCALE,
  t: (key, vars) => translate(DEFAULT_LOCALE, key, vars),
  setLocale: () => undefined,
});

export function useTranslation(): I18nValue {
  return useContext(I18nContext);
}
