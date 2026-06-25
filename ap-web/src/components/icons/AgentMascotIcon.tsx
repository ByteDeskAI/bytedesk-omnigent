import { forwardRef, type SVGProps } from "react";
import agentMascotUrl from "@/assets/agent-mascot.png";

export const AGENT_MASCOT_VIEWBOX_WIDTH = 1023;
export const AGENT_MASCOT_VIEWBOX_HEIGHT = 1070;

export const AGENT_MASCOT_EYE_CENTERS = [
  { cx: 384, cy: 588 },
  { cx: 654, cy: 588 },
] as const;

// The base is a generated raster mascot. The live SVG eye layers cover the
// baked-in pupils so AgentMascotEyes can move the visible pupils independently.
export const AgentMascotIcon = forwardRef<SVGSVGElement, SVGProps<SVGSVGElement>>(
  function AgentMascotIcon(props, ref) {
    return (
      <svg
        ref={ref}
        viewBox={`0 0 ${AGENT_MASCOT_VIEWBOX_WIDTH} ${AGENT_MASCOT_VIEWBOX_HEIGHT}`}
        aria-hidden="true"
        {...props}
      >
        <image
          href={agentMascotUrl}
          width={AGENT_MASCOT_VIEWBOX_WIDTH}
          height={AGENT_MASCOT_VIEWBOX_HEIGHT}
          preserveAspectRatio="xMidYMid meet"
        />

        <g className="otto-eye">
          <ellipse
            cx={AGENT_MASCOT_EYE_CENTERS[0].cx}
            cy={AGENT_MASCOT_EYE_CENTERS[0].cy}
            rx="67"
            ry="82"
            fill="#f8fbff"
            stroke="#e7edf4"
            strokeWidth="4"
          />
          <g className="otto-pupil">
            <circle
              cx={AGENT_MASCOT_EYE_CENTERS[0].cx}
              cy={AGENT_MASCOT_EYE_CENTERS[0].cy + 8}
              r="35"
              fill="#06121d"
            />
            <path
              d="M358 626c13 11 39 11 52 0"
              fill="none"
              stroke="#2ae9f4"
              strokeLinecap="round"
              strokeWidth="8"
            />
            <circle cx="405" cy="559" r="13" fill="#fff" />
          </g>
        </g>

        <g className="otto-eye">
          <ellipse
            cx={AGENT_MASCOT_EYE_CENTERS[1].cx}
            cy={AGENT_MASCOT_EYE_CENTERS[1].cy}
            rx="67"
            ry="82"
            fill="#f8fbff"
            stroke="#e7edf4"
            strokeWidth="4"
          />
          <g className="otto-pupil">
            <circle
              cx={AGENT_MASCOT_EYE_CENTERS[1].cx}
              cy={AGENT_MASCOT_EYE_CENTERS[1].cy + 8}
              r="35"
              fill="#06121d"
            />
            <path
              d="M628 626c13 11 39 11 52 0"
              fill="none"
              stroke="#2ae9f4"
              strokeLinecap="round"
              strokeWidth="8"
            />
            <circle cx="675" cy="559" r="13" fill="#fff" />
          </g>
        </g>
      </svg>
    );
  },
);
