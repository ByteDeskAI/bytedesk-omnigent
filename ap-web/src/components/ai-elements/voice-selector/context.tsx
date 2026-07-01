"use client";

import { createContext, useContext } from "react";

export interface VoiceSelectorContextValue {
  value: string | undefined;
  setValue: (value: string | undefined) => void;
  open: boolean;
  setOpen: (open: boolean) => void;
}

export const VoiceSelectorContext = createContext<VoiceSelectorContextValue | null>(null);

export const useVoiceSelector = () => {
  const context = useContext(VoiceSelectorContext);
  if (!context) {
    throw new Error("VoiceSelector components must be used within VoiceSelector");
  }
  return context;
};