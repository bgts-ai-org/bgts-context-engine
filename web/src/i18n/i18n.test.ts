import { describe, expect, it } from "vitest";

import { en } from "./en";
import { tr } from "./tr";
import { DEFAULT_LOCALE, LOCALES, isLocale, translate } from ".";

describe("catalogs", () => {
  it("English is the default locale", () => {
    expect(DEFAULT_LOCALE).toBe("en");
  });

  it("cover exactly the same keys", () => {
    expect(Object.keys(tr).sort()).toEqual(Object.keys(en).sort());
  });

  it("have no blank entries", () => {
    for (const locale of LOCALES) {
      const catalog = locale === "en" ? (en as Record<string, string>) : tr;
      const blank = Object.entries(catalog)
        .filter(([, value]) => value.trim() === "")
        .map(([key]) => key);
      expect(blank, `blank ${locale} entries`).toEqual([]);
    }
  });

  it("use matching placeholders in every locale", () => {
    const placeholders = (value: string) =>
      [...value.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();

    for (const key of Object.keys(en) as (keyof typeof en)[]) {
      expect(placeholders(tr[key]), `placeholders for ${key}`).toEqual(placeholders(en[key]));
    }
  });
});

describe("translate", () => {
  it("substitutes placeholders", () => {
    expect(translate("en", "detail.connections", { count: 7 })).toBe("Connections (7)");
  });

  it("substitutes the same placeholder everywhere it appears", () => {
    const result = translate("en", "steps.expand.subtitle", {
      count: 3,
      distance: 2,
      total: 9,
    });
    expect(result).toBe("3 new nodes discovered at distance 2 (9 in total)");
  });

  it("leaves unknown placeholders untouched rather than printing undefined", () => {
    expect(translate("en", "detail.connections")).toBe("Connections ({count})");
  });

  it("translates into Turkish", () => {
    expect(translate("tr", "nav.explore")).toBe("Keşfet");
  });
});

describe("isLocale", () => {
  it("accepts supported locales", () => {
    expect(isLocale("en")).toBe(true);
    expect(isLocale("tr")).toBe(true);
  });

  it("rejects anything else", () => {
    expect(isLocale("de")).toBe(false);
    expect(isLocale("")).toBe(false);
    expect(isLocale(null)).toBe(false);
    expect(isLocale(undefined)).toBe(false);
  });
});
