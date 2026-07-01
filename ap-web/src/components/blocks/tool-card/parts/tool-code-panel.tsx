import {
  CodeBlock,
  CodeBlockActions,
  CodeBlockHeader,
  CodeBlockTitle,
} from "@/components/ai-elements/code-block";
import { CopyTextButton } from "./copy-text-button";

export function CodePanel({
  title,
  text,
  copyText,
  copyLabel,
}: {
  title: string;
  text: string;
  copyText: string;
  copyLabel: string;
}) {
  return (
    <CodeBlock code={text} language="json">
      <CodeBlockHeader>
        <CodeBlockTitle className="min-w-0">
          <span className="truncate font-medium uppercase tracking-wide">{title}</span>
        </CodeBlockTitle>
        <CodeBlockActions>
          <CopyTextButton label={copyLabel} text={copyText} />
        </CodeBlockActions>
      </CodeBlockHeader>
    </CodeBlock>
  );
}