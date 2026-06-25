import { useEffect, useRef } from "react";
import {
  AGENT_MASCOT_EYE_CENTERS,
  AGENT_MASCOT_VIEWBOX_HEIGHT,
  AGENT_MASCOT_VIEWBOX_WIDTH,
  AgentMascotIcon,
} from "@/components/icons/AgentMascotIcon";

const WHITE_RADIUS = 67;
const PUPIL_RADIUS = 35;
const MAX_OFFSET = Math.min(13, WHITE_RADIUS - PUPIL_RADIUS);

export function AgentMascotEyes({
  className,
  decorative = false,
}: {
  className?: string;
  decorative?: boolean;
}) {
  const svgRef = useRef<SVGSVGElement>(null);

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;

    const pupils = Array.from(svg.querySelectorAll<SVGGElement>("g.otto-pupil"));
    for (const pupil of pupils) {
      pupil.style.transition = "transform 90ms ease-out";
      pupil.style.willChange = "transform";
    }

    let frame = 0;
    let pointer: { x: number; y: number } | null = null;

    const apply = () => {
      frame = 0;
      if (!pointer) return;
      const rect = svg.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return;

      AGENT_MASCOT_EYE_CENTERS.forEach((eye, i) => {
        const pupil = pupils[i];
        if (!pupil) return;
        const eyeX = rect.left + (eye.cx / AGENT_MASCOT_VIEWBOX_WIDTH) * rect.width;
        const eyeY = rect.top + (eye.cy / AGENT_MASCOT_VIEWBOX_HEIGHT) * rect.height;
        const dx = pointer!.x - eyeX;
        const dy = pointer!.y - eyeY;
        const dist = Math.hypot(dx, dy);
        if (dist < 0.0001) {
          pupil.style.transform = "translate(0px, 0px)";
          return;
        }

        const tx = (dx / dist) * MAX_OFFSET;
        const ty = (dy / dist) * MAX_OFFSET;
        pupil.style.transform = `translate(${tx.toFixed(3)}px, ${ty.toFixed(3)}px)`;
      });
    };

    const onMove = (e: PointerEvent) => {
      pointer = { x: e.clientX, y: e.clientY };
      if (!frame) frame = requestAnimationFrame(apply);
    };

    window.addEventListener("pointermove", onMove, { passive: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      if (frame) cancelAnimationFrame(frame);
    };
  }, []);

  return (
    <AgentMascotIcon
      ref={svgRef}
      className={className}
      role={decorative ? undefined : "img"}
      aria-label={decorative ? undefined : "Omnigent agent"}
      aria-hidden={decorative}
    />
  );
}
