/// <reference types="node" />
// Node types via explicit reference: the app tsconfig is browser-only, and
// importing index.css?raw instead yields "" under vitest's CSS stubbing.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// Relative to the vitest root (ap-web/) — import.meta.url is not a file://
// URL inside vitest's module graph, so it can't locate the file.
const cssSource = readFileSync("src/index.css", "utf8");

/** Innermost `selector { ... }` blocks. */
function extractRules(css: string): string[] {
  return css.match(/[^{}]+\{[^{}]*\}/g) ?? [];
}

function findRuleContaining(...needles: string[]): string {
  const rule = extractRules(cssSource).find((candidate) =>
    needles.every((needle) => candidate.includes(needle)),
  );
  if (!rule) {
    throw new Error(`Could not find CSS rule containing: ${needles.join(", ")}`);
  }
  return rule;
}

describe("index.css Mission Control surface rules", () => {
  it("uses flat tokenized card surfaces instead of frosted glass", () => {
    const rule = findRuleContaining(".bg-card", "data-collapsed");

    expect(rule).toContain("border: 1px solid var(--color-border-default)");
    expect(rule).toContain("box-shadow: var(--shadow-sm)");
    expect(rule).not.toMatch(/(?<![-\w])backdrop-filter\s*:/);
    expect(rule).not.toMatch(/-webkit-backdrop-filter\s*:/);
  });

  it("uses raised tokenized popover/menu surfaces", () => {
    const rule = findRuleContaining('[data-slot="popover-content"]', '[role="menu"]');

    expect(rule).toContain("border: 1px solid var(--color-border-stronger)");
    expect(rule).toContain("box-shadow: var(--shadow-md)");
    expect(rule).not.toMatch(/(?<![-\w])backdrop-filter\s*:/);
    expect(rule).not.toMatch(/-webkit-backdrop-filter\s*:/);
  });
});

/* Regression test for the "page gets wider when the kebab menu opens" bug.
 *
 * The bg-card surface rule used to exclude `[aria-hidden="true"]` to skip
 * visually collapsed panels. But Radix's modal a11y hiding sets
 * aria-hidden="true" on the OPEN sidebar while a menu/dialog is up, which
 * dropped the rule's 1px border and reflowed every sidebar row 2px wider
 * (titles gained a character). The rule now keys on `data-collapsed`,
 * which only the panels themselves set. This test runs the actual selector
 * from index.css against a real DOM to pin that contract.
 */
describe("index.css bg-card surface rule selector", () => {
  // The selector of the rule declaring the top-level bg-card surface border.
  const cardRule = findRuleContaining(".bg-card", "data-collapsed");
  // Strip comments preceding the selector in the extracted block.
  const selector = cardRule
    .slice(0, cardRule.indexOf("{"))
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .trim();

  function makeAside(): HTMLElement {
    const dark = document.createElement("div");
    dark.className = "dark";
    const aside = document.createElement("aside");
    aside.className = "conversations-sidebar flex flex-col bg-card";
    dark.appendChild(aside);
    document.body.appendChild(dark);
    return aside;
  }

  it("matches an open bg-card panel even while Radix marks it aria-hidden", () => {
    const aside = makeAside();
    expect(aside.matches(selector)).toBe(true);
    aside.setAttribute("aria-hidden", "true");
    expect(aside.matches(selector)).toBe(true);
    aside.remove();
  });

  it("stops matching when the panel marks itself collapsed", () => {
    const aside = makeAside();
    aside.setAttribute("data-collapsed", "true");
    expect(aside.matches(selector)).toBe(false);
    aside.remove();
  });
});
