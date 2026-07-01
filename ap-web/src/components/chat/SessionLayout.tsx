interface SessionLayoutProps {
  mainAgent: React.ReactNode;
}

/**
 * Inside a conversation: wraps the chat surface. The terminals panel
 * and right rail are managed by AppShell and rendered outside this
 * component as flex siblings.
 */
export function SessionLayout({ mainAgent }: SessionLayoutProps) {
  return (
    <div className="flex min-h-0 flex-1 overflow-hidden">
      <div className="flex min-w-0 flex-1 flex-col">{mainAgent}</div>
    </div>
  );
}