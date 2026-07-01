"use client";

import { cn } from "@/lib/utils";
import type { ComponentProps } from "react";
import { useContext } from "react";
import { deviceIdRegex, MicSelectorContext } from "./context";

export type MicSelectorLabelProps = ComponentProps<"span"> & {
  device: MediaDeviceInfo;
};

export const MicSelectorLabel = ({ device, className, ...props }: MicSelectorLabelProps) => {
  const matches = device.label.match(deviceIdRegex);

  if (!matches) {
    return (
      <span className={className} {...props}>
        {device.label}
      </span>
    );
  }

  const [, deviceId] = matches;
  const name = device.label.replace(deviceIdRegex, "");

  return (
    <span className={className} {...props}>
      <span>{name}</span>
      <span className="text-muted-foreground"> ({deviceId})</span>
    </span>
  );
};

export type MicSelectorValueProps = ComponentProps<"span">;

export const MicSelectorValue = ({ className, ...props }: MicSelectorValueProps) => {
  const { data, value } = useContext(MicSelectorContext);
  const currentDevice = data.find((d) => d.deviceId === value);

  if (!currentDevice) {
    return (
      <span className={cn("flex-1 text-left", className)} {...props}>
        Select microphone...
      </span>
    );
  }

  return (
    <MicSelectorLabel
      className={cn("flex-1 text-left", className)}
      device={currentDevice}
      {...props}
    />
  );
};