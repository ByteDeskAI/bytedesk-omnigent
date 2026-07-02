/**
 * Console motion presets.
 *
 * `motion` (v12) ships with the app but isn't wired in yet — the redesign uses
 * it for state-change transitions only (page/stage swaps, telemetry readouts,
 * rail indicator, bubble entrance, panel slides), never ambient decoration.
 *
 * Every preset is authored so that under `prefers-reduced-motion` the element
 * still mounts in its final state with no transform/opacity animation. Consume
 * `reducedMotion` from `useConsoleMotion()` to pick the right variant set, or
 * pass the tokens below straight to `motion.*` components.
 */
import { useReducedMotion } from "motion/react";
import type { Transition, Variants } from "motion/react";

/** Durations/easings mirror the CSS token scale in bytedesk-mission-control.css. */
export const MOTION = {
  fast: 0.12,
  normal: 0.2,
  slow: 0.3,
  enter: 0.25,
  easeOut: [0.4, 0, 0.2, 1],
  spring: [0.34, 1.56, 0.64, 1],
} as const;

export const springTransition: Transition = {
  type: "spring",
  stiffness: 420,
  damping: 32,
};

/** Instant variants for reduced-motion: mount in final state, no movement. */
const STILL: Variants = {
  hidden: { opacity: 1 },
  visible: { opacity: 1, transition: { duration: 0 } },
  exit: { opacity: 1, transition: { duration: 0 } },
};

/** Fade + 8px rise. Stage/page swaps, bubble entrance. */
const FADE_RISE: Variants = {
  hidden: { opacity: 0, y: 8 },
  visible: { opacity: 1, y: 0, transition: { duration: MOTION.enter, ease: MOTION.easeOut } },
  exit: { opacity: 0, y: -4, transition: { duration: MOTION.fast, ease: MOTION.easeOut } },
};

/** Scale + fade. Command palette, popovers, tray. */
const SCALE_FADE: Variants = {
  hidden: { opacity: 0, scale: 0.98 },
  visible: { opacity: 1, scale: 1, transition: { duration: MOTION.normal, ease: MOTION.easeOut } },
  exit: { opacity: 0, scale: 0.98, transition: { duration: MOTION.fast, ease: MOTION.easeOut } },
};

export interface ConsoleMotion {
  reducedMotion: boolean;
  fadeRise: Variants;
  scaleFade: Variants;
  /** Transition for values that should animate their layout (rail indicator). */
  layout: Transition;
}

/**
 * Returns the motion preset set appropriate to the user's reduced-motion
 * preference. When reduced motion is on, every variant resolves to STILL and
 * layout transitions are instant.
 */
export function useConsoleMotion(): ConsoleMotion {
  const reduced = useReducedMotion() ?? false;
  return {
    reducedMotion: reduced,
    fadeRise: reduced ? STILL : FADE_RISE,
    scaleFade: reduced ? STILL : SCALE_FADE,
    layout: reduced ? { duration: 0 } : springTransition,
  };
}

export { FADE_RISE, SCALE_FADE, STILL };
