import { useStickToBottomContext } from "use-stick-to-bottom";
import { UserMessageNav } from "@/components/UserMessageNav";
import { cn } from "@/lib/utils";

/**
 * Adds scroll-state CSS classes to UserMessageNav.
 */
export function UserMessageNavConnected(props: React.ComponentProps<typeof UserMessageNav>) {
  const { isAtBottom } = useStickToBottomContext();
  return (
    <UserMessageNav {...props} className={cn(props.className, isAtBottom && "max-md:hidden")} />
  );
}