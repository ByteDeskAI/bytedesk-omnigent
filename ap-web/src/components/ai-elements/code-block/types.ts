import type { HTMLAttributes } from "react";
import type { BundledLanguage, ThemedToken } from "shiki";

export type CodeBlockProps = HTMLAttributes<HTMLDivElement> & {
  code: string;
  language: BundledLanguage;
  showLineNumbers?: boolean;
};

export interface TokenizedCode {
  tokens: ThemedToken[][];
  fg: string;
  bg: string;
}

export interface CodeBlockContextType {
  code: string;
}

export interface KeyedToken {
  token: ThemedToken;
  key: string;
}

export interface KeyedLine {
  tokens: KeyedToken[];
  key: string;
}