import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function CodeViewerMarkdownPreview({ content }: { content: string }) {
  return (
    <div className="px-6 py-4 overflow-auto h-full prose dark:prose-invert prose-sm max-w-none">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  );
}