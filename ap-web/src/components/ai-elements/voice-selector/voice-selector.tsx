"use client";

import { useControllableState } from "@radix-ui/react-use-controllable-state";
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
  CommandShortcut,
} from "@/components/ui/command";
import { Dialog, DialogContent, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import type { ComponentProps, ReactNode } from "react";
import { useMemo } from "react";

import { VoiceSelectorContext } from "./context";

export type VoiceSelectorProps = ComponentProps<typeof Dialog> & {
  value?: string;
  defaultValue?: string;
  onValueChange?: (value: string | undefined) => void;
};

export const VoiceSelector = ({
  value: valueProp,
  defaultValue,
  onValueChange,
  open: openProp,
  defaultOpen = false,
  onOpenChange,
  children,
  ...props
}: VoiceSelectorProps) => {
  const [value, setValue] = useControllableState({
    defaultProp: defaultValue,
    onChange: onValueChange,
    prop: valueProp,
  });

  const [open, setOpen] = useControllableState({
    defaultProp: defaultOpen,
    onChange: onOpenChange,
    prop: openProp,
  });

  const voiceSelectorContext = useMemo(
    () => ({ open, setOpen, setValue, value }),
    [value, setValue, open, setOpen],
  );

  return (
    <VoiceSelectorContext.Provider value={voiceSelectorContext}>
      <Dialog onOpenChange={setOpen} open={open} {...props}>
        {children}
      </Dialog>
    </VoiceSelectorContext.Provider>
  );
};

export type VoiceSelectorTriggerProps = ComponentProps<typeof DialogTrigger>;

export const VoiceSelectorTrigger = (props: VoiceSelectorTriggerProps) => (
  <DialogTrigger {...props} />
);

export type VoiceSelectorContentProps = ComponentProps<typeof DialogContent> & {
  title?: ReactNode;
};

export const VoiceSelectorContent = ({
  className,
  children,
  title = "Voice Selector",
  ...props
}: VoiceSelectorContentProps) => (
  <DialogContent aria-describedby={undefined} className={cn("p-0", className)} {...props}>
    <DialogTitle className="sr-only">{title}</DialogTitle>
    <Command className="**:data-[slot=command-input-wrapper]:h-auto">{children}</Command>
  </DialogContent>
);

export type VoiceSelectorDialogProps = ComponentProps<typeof CommandDialog>;

export const VoiceSelectorDialog = (props: VoiceSelectorDialogProps) => (
  <CommandDialog {...props} />
);

export type VoiceSelectorInputProps = ComponentProps<typeof CommandInput>;

export const VoiceSelectorInput = ({ className, ...props }: VoiceSelectorInputProps) => (
  <CommandInput className={cn("h-auto py-3.5", className)} {...props} />
);

export type VoiceSelectorListProps = ComponentProps<typeof CommandList>;

export const VoiceSelectorList = (props: VoiceSelectorListProps) => <CommandList {...props} />;

export type VoiceSelectorEmptyProps = ComponentProps<typeof CommandEmpty>;

export const VoiceSelectorEmpty = (props: VoiceSelectorEmptyProps) => <CommandEmpty {...props} />;

export type VoiceSelectorGroupProps = ComponentProps<typeof CommandGroup>;

export const VoiceSelectorGroup = (props: VoiceSelectorGroupProps) => <CommandGroup {...props} />;

export type VoiceSelectorItemProps = ComponentProps<typeof CommandItem>;

export const VoiceSelectorItem = ({ className, ...props }: VoiceSelectorItemProps) => (
  <CommandItem className={cn("px-4 py-2", className)} {...props} />
);

export type VoiceSelectorShortcutProps = ComponentProps<typeof CommandShortcut>;

export const VoiceSelectorShortcut = (props: VoiceSelectorShortcutProps) => (
  <CommandShortcut {...props} />
);

export type VoiceSelectorSeparatorProps = ComponentProps<typeof CommandSeparator>;

export const VoiceSelectorSeparator = (props: VoiceSelectorSeparatorProps) => (
  <CommandSeparator {...props} />
);