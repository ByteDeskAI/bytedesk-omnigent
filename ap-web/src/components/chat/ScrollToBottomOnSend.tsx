import { useLayoutEffect } from "react";
import { useStickToBottomContext } from "use-stick-to-bottom";

/**
 * Forces the conversation back to the bottom when this client submits a
 * new message.
 */
export function ScrollToBottomOnSend({ nonce }: { nonce: number }) {
  const { scrollToBottom } = useStickToBottomContext();

  useLayoutEffect(() => {
    if (nonce === 0) return;
    scrollToBottom("instant");
    requestAnimationFrame(() => scrollToBottom("instant"));
  }, [nonce, scrollToBottom]);

  return null;
}