import type { CSSProperties } from "react";
import type { ThemedToken } from "shiki";

import type { KeyedLine } from "./types";

// Shiki uses bitflags for font styles: 1=italic, 2=bold, 4=underline
// oxlint-disable-next-line eslint(no-bitwise)
export const isItalic = (fontStyle: number | undefined) => fontStyle && fontStyle & 1;
// oxlint-disable-next-line eslint(no-bitwise)
export const isBold = (fontStyle: number | undefined) => fontStyle && fontStyle & 2;
export const isUnderline = (fontStyle: number | undefined) =>
  // oxlint-disable-next-line eslint(no-bitwise)
  fontStyle && fontStyle & 4;

export const addKeysToTokens = (lines: ThemedToken[][]): KeyedLine[] =>
  lines.map((line, lineIdx) => ({
    key: `line-${lineIdx}`,
    tokens: line.map((token, tokenIdx) => ({
      key: `line-${lineIdx}-${tokenIdx}`,
      token,
    })),
  }));

export const getTokenStyle = (token: ThemedToken): CSSProperties =>
  ({
    backgroundColor: token.bgColor,
    color: token.color,
    fontStyle: isItalic(token.fontStyle) ? "italic" : undefined,
    fontWeight: isBold(token.fontStyle) ? "bold" : undefined,
    textDecoration: isUnderline(token.fontStyle) ? "underline" : undefined,
    ...token.htmlStyle,
  }) as CSSProperties;

// Line number styles using CSS counters
export const LINE_NUMBER_CLASSES =
  "block before:content-[counter(line)] before:inline-block before:[counter-increment:line] before:w-8 before:mr-4 before:text-right before:text-muted-foreground/50 before:font-mono before:select-none";