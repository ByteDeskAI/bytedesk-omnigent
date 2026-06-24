// Minimal SSE line reader over a platform ReadableStream<Uint8Array>. Splits the
// byte stream into lines (\n / \r\n / \r), tolerant of chunk boundaries that cut a
// line — or a \r\n pair — in half. Used by the events port to drive the
// event:/id:/data: frame loop. Zero deps — TextDecoder + ReadableStream are
// platform APIs.

/** Yield decoded lines from a byte stream, without trailing newline characters. */
export async function* readLines(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<string> {
  const reader = stream.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  // True when the previous chunk ended on a bare '\r' whose line was already
  // emitted; a '\n' starting the next chunk is the back half of a split CRLF and
  // must be swallowed (not treated as an empty line).
  let swallowLeadingLf = false;

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let start = 0;
      if (swallowLeadingLf && buffer[0] === "\n") start = 1;
      swallowLeadingLf = false;

      let i = start;
      while (i < buffer.length) {
        const c = buffer[i];
        if (c === "\n") {
          yield buffer.slice(start, i);
          i += 1;
          start = i;
        } else if (c === "\r") {
          yield buffer.slice(start, i);
          if (buffer[i + 1] === "\n") {
            i += 2; // CRLF fully present in this buffer
          } else if (i + 1 === buffer.length) {
            // '\r' is the last char — could be a split CRLF. Emit the line now,
            // swallow a leading '\n' on the next chunk.
            i += 1;
            swallowLeadingLf = true;
          } else {
            i += 1; // bare CR line ending
          }
          start = i;
        } else {
          i += 1;
        }
      }
      buffer = buffer.slice(start);
    }

    // Flush any trailing partial line not terminated by a newline.
    buffer += decoder.decode();
    if (swallowLeadingLf && buffer.startsWith("\n")) buffer = buffer.slice(1);
    if (buffer.length > 0) yield buffer;
  } finally {
    reader.releaseLock();
  }
}
