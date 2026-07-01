"use client";

import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import { PauseIcon, PlayIcon } from "lucide-react";
import type { ComponentProps } from "react";
import { useCallback } from "react";

export type VoiceSelectorAgeProps = ComponentProps<"span">;

export const VoiceSelectorAge = ({ className, ...props }: VoiceSelectorAgeProps) => (
  <span className={cn("text-muted-foreground text-xs tabular-nums", className)} {...props} />
);

export type VoiceSelectorNameProps = ComponentProps<"span">;

export const VoiceSelectorName = ({ className, ...props }: VoiceSelectorNameProps) => (
  <span className={cn("flex-1 truncate text-left font-medium", className)} {...props} />
);

export type VoiceSelectorDescriptionProps = ComponentProps<"span">;

export const VoiceSelectorDescription = ({
  className,
  ...props
}: VoiceSelectorDescriptionProps) => (
  <span className={cn("text-muted-foreground text-xs", className)} {...props} />
);

export type VoiceSelectorAttributesProps = ComponentProps<"div">;

export const VoiceSelectorAttributes = ({
  className,
  children,
  ...props
}: VoiceSelectorAttributesProps) => (
  <div className={cn("flex items-center text-xs", className)} {...props}>
    {children}
  </div>
);

export type VoiceSelectorBulletProps = ComponentProps<"span">;

export const VoiceSelectorBullet = ({ className, ...props }: VoiceSelectorBulletProps) => (
  <span aria-hidden="true" className={cn("select-none text-border", className)} {...props}>
    &bull;
  </span>
);

export type VoiceSelectorPreviewProps = Omit<ComponentProps<"button">, "children"> & {
  playing?: boolean;
  loading?: boolean;
  onPlay?: () => void;
};

export const VoiceSelectorPreview = ({
  className,
  playing,
  loading,
  onPlay,
  onClick,
  ...props
}: VoiceSelectorPreviewProps) => {
  const handleClick = useCallback(
    (event: React.MouseEvent<HTMLButtonElement>) => {
      event.stopPropagation();
      onClick?.(event);
      onPlay?.();
    },
    [onClick, onPlay],
  );

  let icon = <PlayIcon className="size-3" />;

  if (loading) {
    icon = <Spinner className="size-3" />;
  } else if (playing) {
    icon = <PauseIcon className="size-3" />;
  }

  return (
    <Button
      aria-label={playing ? "Pause preview" : "Play preview"}
      className={cn("size-6", className)}
      disabled={loading}
      onClick={handleClick}
      size="icon-sm"
      type="button"
      variant="outline"
      {...props}
    >
      {icon}
    </Button>
  );
};