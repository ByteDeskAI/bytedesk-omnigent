"use client";

import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { cn } from "@/lib/utils";
import { GitCommitIcon } from "lucide-react";
import type { ComponentProps, HTMLAttributes } from "react";
import { useCallback, useEffect, useState } from "react";

export type CommitHashProps = HTMLAttributes<HTMLSpanElement>;

export const CommitHash = ({ className, children, ...props }: CommitHashProps) => (
  <span className={cn("font-mono text-xs", className)} {...props}>
    <GitCommitIcon className="mr-1 inline-block size-3" />
    {children}
  </span>
);

export type CommitMessageProps = HTMLAttributes<HTMLSpanElement>;

export const CommitMessage = ({ className, children, ...props }: CommitMessageProps) => (
  <span className={cn("font-medium text-sm", className)} {...props}>
    {children}
  </span>
);

export type CommitMetadataProps = HTMLAttributes<HTMLDivElement>;

export const CommitMetadata = ({ className, children, ...props }: CommitMetadataProps) => (
  <div
    className={cn("flex items-center gap-2 text-muted-foreground text-xs", className)}
    {...props}
  >
    {children}
  </div>
);

export type CommitSeparatorProps = HTMLAttributes<HTMLSpanElement>;

export const CommitSeparator = ({ className, children, ...props }: CommitSeparatorProps) => (
  <span className={className} {...props}>
    {children ?? "•"}
  </span>
);

export type CommitInfoProps = HTMLAttributes<HTMLDivElement>;

export const CommitInfo = ({ className, children, ...props }: CommitInfoProps) => (
  <div className={cn("flex flex-1 flex-col", className)} {...props}>
    {children}
  </div>
);

export type CommitAuthorProps = HTMLAttributes<HTMLDivElement>;

export const CommitAuthor = ({ className, children, ...props }: CommitAuthorProps) => (
  <div className={cn("flex items-center", className)} {...props}>
    {children}
  </div>
);

export type CommitAuthorAvatarProps = ComponentProps<typeof Avatar> & {
  initials: string;
};

export const CommitAuthorAvatar = ({ initials, className, ...props }: CommitAuthorAvatarProps) => (
  <Avatar className={cn("size-8", className)} {...props}>
    <AvatarFallback className="text-xs">{initials}</AvatarFallback>
  </Avatar>
);

export type CommitTimestampProps = HTMLAttributes<HTMLTimeElement> & {
  date: Date;
};

const relativeTimeFormat = new Intl.RelativeTimeFormat("en", {
  numeric: "auto",
});

const formatRelativeDate = (date: Date) => {
  const days = Math.round((date.getTime() - Date.now()) / (1000 * 60 * 60 * 24));
  return relativeTimeFormat.format(days, "day");
};

export const CommitTimestamp = ({ date, className, children, ...props }: CommitTimestampProps) => {
  const [formatted, setFormatted] = useState("");

  const updateFormatted = useCallback(() => {
    setFormatted(formatRelativeDate(date));
  }, [date]);

  useEffect(() => {
    updateFormatted();
  }, [updateFormatted]);

  return (
    <time className={cn("text-xs", className)} dateTime={date.toISOString()} {...props}>
      {children ?? formatted}
    </time>
  );
};