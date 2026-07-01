"use client";

import { useControllableState } from "@radix-ui/react-use-controllable-state";
import { Popover } from "@/components/ui/popover";
import type { ComponentProps } from "react";
import { useEffect, useMemo, useState } from "react";
import { MicSelectorContext } from "./context";
import { useAudioDevices } from "./use-audio-devices";

export type MicSelectorProps = ComponentProps<typeof Popover> & {
  defaultValue?: string;
  value?: string | undefined;
  onValueChange?: (value: string | undefined) => void;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
};

export const MicSelector = ({
  defaultValue,
  value: controlledValue,
  onValueChange: controlledOnValueChange,
  defaultOpen = false,
  open: controlledOpen,
  onOpenChange: controlledOnOpenChange,
  ...props
}: MicSelectorProps) => {
  const [value, onValueChange] = useControllableState<string | undefined>({
    defaultProp: defaultValue,
    onChange: controlledOnValueChange,
    prop: controlledValue,
  });
  const [open, onOpenChange] = useControllableState({
    defaultProp: defaultOpen,
    onChange: controlledOnOpenChange,
    prop: controlledOpen,
  });
  const [width, setWidth] = useState(200);
  const { devices, loading, hasPermission, loadDevices } = useAudioDevices();

  useEffect(() => {
    if (open && !hasPermission && !loading) {
      loadDevices();
    }
  }, [open, hasPermission, loading, loadDevices]);

  const contextValue = useMemo(
    () => ({
      data: devices,
      onOpenChange,
      onValueChange,
      open,
      setWidth,
      value,
      width,
    }),
    [devices, onOpenChange, onValueChange, open, setWidth, value, width],
  );

  return (
    <MicSelectorContext.Provider value={contextValue}>
      <Popover {...props} onOpenChange={onOpenChange} open={open} />
    </MicSelectorContext.Provider>
  );
};