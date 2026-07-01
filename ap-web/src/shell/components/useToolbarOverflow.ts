import { useLayoutEffect, useRef, useState } from "react";
import { TOOLBAR_MAX_TITLE_PX, TOOLBAR_MIN_TITLE_PX } from "./file-viewer/fileViewerToolbarConstants";

export function useToolbarOverflow(actionsKey: string): {
  headerRef: React.RefObject<HTMLDivElement | null>;
  backRef: React.RefObject<HTMLDivElement | null>;
  navRef: React.RefObject<HTMLDivElement | null>;
  pathMeasureRef: React.RefObject<HTMLSpanElement | null>;
  chipRef: React.RefObject<HTMLSpanElement | null>;
  measureRef: React.RefObject<HTMLDivElement | null>;
  collapsed: boolean;
} {
  const headerRef = useRef<HTMLDivElement | null>(null);
  const backRef = useRef<HTMLDivElement | null>(null);
  const navRef = useRef<HTMLDivElement | null>(null);
  const pathMeasureRef = useRef<HTMLSpanElement | null>(null);
  const chipRef = useRef<HTMLSpanElement | null>(null);
  const measureRef = useRef<HTMLDivElement | null>(null);
  const [collapsed, setCollapsed] = useState(false);

  useLayoutEffect(() => {
    const header = headerRef.current;
    const measure = measureRef.current;
    if (!header || !measure || typeof ResizeObserver === "undefined") return;
    const evaluate = () => {
      const style = getComputedStyle(header);
      const padX = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
      const available = header.clientWidth - padX;
      if (available <= 0) return;

      const backWidth = backRef.current?.offsetWidth ?? 0;
      const navWidth = navRef.current?.offsetWidth ?? 0;
      const chipWidth = chipRef.current?.offsetWidth ?? 0;
      const buttonsWidth = measure.scrollWidth;
      const pathNatural = pathMeasureRef.current?.offsetWidth ?? 0;
      const titleReserve = Math.min(
        Math.max(pathNatural, TOOLBAR_MIN_TITLE_PX),
        TOOLBAR_MAX_TITLE_PX,
      );
      const required = backWidth + navWidth + titleReserve + chipWidth + buttonsWidth + 12;
      setCollapsed(available < required);
    };
    evaluate();
    const ro = new ResizeObserver(evaluate);
    ro.observe(header);
    return () => ro.disconnect();
  }, [actionsKey]);

  return { headerRef, backRef, navRef, pathMeasureRef, chipRef, measureRef, collapsed };
}