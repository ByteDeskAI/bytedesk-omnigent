"use client";

import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { PopoverContent } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import type { ComponentProps, ReactNode } from "react";
import { useCallback, useContext } from "react";
import { MicSelectorContext } from "./context";

export type MicSelectorContentProps = ComponentProps<typeof Command> & {
  popoverOptions?: ComponentProps<typeof PopoverContent>;
};

export const MicSelectorContent = ({
  className,
  popoverOptions,
  ...props
}: MicSelectorContentProps) => {
  const { width, onValueChange, value } = useContext(MicSelectorContext);

  return (
    <PopoverContent className={cn("p-0", className)} style={{ width }} {...popoverOptions}>
      <Command onValueChange={onValueChange} value={value} {...props} />
    </PopoverContent>
  );
};

export type MicSelectorInputProps = ComponentProps<typeof CommandInput> & {
  value?: string;
  defaultValue?: string;
  onValueChange?: (value: string) => void;
};

export const MicSelectorInput = ({ ...props }: MicSelectorInputProps) => (
  <CommandInput placeholder="Search microphones..." {...props} />
);

export type MicSelectorListProps = Omit<ComponentProps<typeof CommandList>, "children"> & {
  children: (devices: MediaDeviceInfo[]) => ReactNode;
};

export const MicSelectorList = ({ children, ...props }: MicSelectorListProps) => {
  const { data } = useContext(MicSelectorContext);

  return <CommandList {...props}>{children(data)}</CommandList>;
};

export type MicSelectorEmptyProps = ComponentProps<typeof CommandEmpty>;

export const MicSelectorEmpty = ({
  children = "No microphone found.",
  ...props
}: MicSelectorEmptyProps) => <CommandEmpty {...props}>{children}</CommandEmpty>;

export type MicSelectorItemProps = ComponentProps<typeof CommandItem>;

export const MicSelectorItem = (props: MicSelectorItemProps) => {
  const { onValueChange, onOpenChange } = useContext(MicSelectorContext);

  const handleSelect = useCallback(
    (currentValue: string) => {
      onValueChange?.(currentValue);
      onOpenChange?.(false);
    },
    [onValueChange, onOpenChange],
  );

  return <CommandItem onSelect={handleSelect} {...props} />;
};