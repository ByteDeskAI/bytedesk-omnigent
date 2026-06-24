// Composable transforms over an OmnigentBlock stream — the TS port of the Python
// client's `_transforms.py` and the C# SDK's OmnigentBlockTransforms. Each is an
// async-iterator wrapper you compose with `pipe`.

import type { OmnigentBlock, OmnigentBlockKind, TextDoneBlock, ResponseEndBlock } from "./blocks.js";

/** A block-stream transform: wraps one async iterable into another. */
export type BlockTransform = (
  stream: AsyncIterable<OmnigentBlock>,
) => AsyncIterable<OmnigentBlock>;

/**
 * Compose transforms left-to-right: `pipe(stream, t1, t2)` is `t2(t1(stream))`.
 * Mirrors `pipe()`.
 */
export function pipe(
  stream: AsyncIterable<OmnigentBlock>,
  ...transforms: BlockTransform[]
): AsyncIterable<OmnigentBlock> {
  for (const t of transforms) {
    stream = t(stream);
  }
  return stream;
}

/** Drop blocks of the given `kind`s. Mirrors `skip_blocks`. */
export function skipBlocks(...kinds: OmnigentBlockKind[]): BlockTransform {
  const drop = new Set(kinds);
  return async function* (stream) {
    for await (const block of stream) {
      if (!drop.has(block.kind)) yield block;
    }
  };
}

/**
 * Suppress `response_end` blocks from tool-loop iterations, yielding only the
 * final one (the one not followed by another block). Mirrors
 * `skip_intermediate_ends`.
 */
export function skipIntermediateEnds(): BlockTransform {
  return async function* (stream) {
    let pendingEnd: ResponseEndBlock | null = null;
    for await (const block of stream) {
      if (block.kind === "response_end") {
        pendingEnd = block;
      } else {
        pendingEnd = null;
        yield block;
      }
    }
    if (pendingEnd !== null) yield pendingEnd;
  };
}

/**
 * Merge `text_done` blocks across tool-loop iterations into a single final one at
 * each `response_end`. Mirrors `merge_text_across_iterations`.
 */
export function mergeTextAcrossIterations(): BlockTransform {
  return async function* (stream) {
    let accumulated = "";
    let lastCtx: TextDoneBlock["ctx"] | null = null;
    for await (const block of stream) {
      if (block.kind === "text_done") {
        accumulated += block.fullText;
        lastCtx = block.ctx;
      } else if (block.kind === "response_end") {
        if (accumulated.length > 0) {
          yield {
            kind: "text_done",
            ctx: block.ctx,
            fullText: accumulated,
            hasCodeBlocks: accumulated.includes("```"),
          };
          accumulated = "";
        }
        yield block;
      } else {
        yield block;
      }
    }
    if (accumulated.length > 0) {
      yield {
        kind: "text_done",
        ctx: lastCtx ?? { agent: null, depth: 0, turn: 0 },
        fullText: accumulated,
        hasCodeBlocks: accumulated.includes("```"),
      };
    }
  };
}

/**
 * Filter to blocks from a specific agent. Pass `null` to include all agents (no
 * filtering). Mirrors `only_agent`.
 */
export function onlyAgent(agentName: string | null): BlockTransform {
  return async function* (stream) {
    for await (const block of stream) {
      if (agentName === null || block.ctx.agent === agentName) yield block;
    }
  };
}
