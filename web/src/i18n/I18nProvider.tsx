import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { api } from "../api";
import {
  I18nContext,
  isLocale,
  readStoredLocale,
  resolveInitialLocale,
  storeLocale,
  translate,
  type Locale,
} from ".";

/**
 * Supplies the active locale to the tree.
 *
 * Starts from the signals available synchronously (query string, stored choice, browser
 * preference) so there is no flash of the wrong language, then adopts the deployment default
 * from /v1/ui/config if the visitor has expressed no preference of their own.
 */
export default function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(() => resolveInitialLocale());

  useEffect(() => {
    if (readStoredLocale() || new URLSearchParams(window.location.search).has("lang")) return;

    let cancelled = false;
    api
      .config()
      .then((config) => {
        if (!cancelled && isLocale(config.default_locale)) {
          setLocaleState(config.default_locale);
        }
      })
      .catch(() => {
        // The API being unreachable is surfaced by the app itself; keep the resolved locale.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const setLocale = useCallback((next: Locale) => {
    storeLocale(next);
    setLocaleState(next);
  }, []);

  const value = useMemo(
    () => ({
      locale,
      setLocale,
      t: (key: Parameters<typeof translate>[1], vars?: Parameters<typeof translate>[2]) =>
        translate(locale, key, vars),
    }),
    [locale, setLocale],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}
