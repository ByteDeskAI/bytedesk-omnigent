import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConversationBubbleList } from "./ConversationBubbleList";
import type { Bubble } from "@/lib/renderItems";

// BubbleView/RunnerStartingIndicator read the module-level chatStore; stub
// them (and keep a real-ish bubbleKey) so this stays a focused unit test.
vi.mock("@/pages/ChatPage", () => ({
  BubbleView: ({ bubble }: { bubble: Bubble }) => (
    <div data-testid="bubble">{bubble.kind}</div>
  ),
  RunnerStartingIndicator: ({ variant }: { variant: string }) => (
    <div data-testid={`runner-${variant}`} />
  ),
  bubbleKey: (b: Bubble) => `${b.kind}:${"itemId" in b ? b.itemId : ""}`,
}));

function userBubble(id: string): Bubble {
  return { kind: "user", itemId: id, content: [] } as Bubble;
}

afterEach(cleanup);

describe("ConversationBubbleList", () => {
  it("renders the empty state when there are no bubbles and nothing working", () => {
    render(
      <ConversationBubbleList
        bubbles={[]}
        showWorkingIndicator={false}
        emptyState={<div data-testid="empty">empty</div>}
      />,
    );
    expect(screen.getByTestId("empty")).toBeInTheDocument();
    expect(screen.queryByTestId("bubble")).toBeNull();
  });

  it("renders the hero launch indicator instead of the empty state when launching", () => {
    render(
      <ConversationBubbleList
        bubbles={[]}
        showWorkingIndicator={false}
        launching
        emptyState={<div data-testid="empty">empty</div>}
      />,
    );
    expect(screen.getByTestId("runner-hero")).toBeInTheDocument();
    expect(screen.queryByTestId("empty")).toBeNull();
  });

  it("renders one BubbleView per bubble", () => {
    render(
      <ConversationBubbleList
        bubbles={[userBubble("a"), userBubble("b")]}
        showWorkingIndicator={false}
        emptyState={<div data-testid="empty">empty</div>}
      />,
    );
    expect(screen.getAllByTestId("bubble")).toHaveLength(2);
    expect(screen.queryByTestId("empty")).toBeNull();
  });

  it("renders the working indicator when showWorkingIndicator is set", () => {
    render(
      <ConversationBubbleList
        bubbles={[userBubble("a")]}
        showWorkingIndicator
        emptyState={<div data-testid="empty">empty</div>}
      />,
    );
    expect(screen.getByTestId("working-indicator")).toBeInTheDocument();
  });
});
