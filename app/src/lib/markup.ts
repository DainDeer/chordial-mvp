// a deliberately tiny inline-markup pass for helper bubbles: the council's
// voices lean on action beats (*ears perk*, **firm mode**) that would
// otherwise render as literal asterisks. this supports exactly emphasis and
// strong - no links, no headings, no html - and everything else stays plain
// text (line breaks already survive via `white-space: pre-wrap`). the output
// is data, not markup: the component maps chunks to react elements, so
// nothing here can inject anything.

export interface Chunk {
  style: "plain" | "em" | "strong";
  text: string;
}

// **strong** first (so its asterisk pairs never half-match as em), then
// *em* - which must not start with whitespace/another asterisk, keeping
// stray math like "2 * 3 * 4" and unmatched asterisks literal
const INLINE = /\*\*([^*]+)\*\*|\*([^\s*][^*]*)\*/g;

export function chunkInline(content: string): Chunk[] {
  const chunks: Chunk[] = [];
  let cursor = 0;
  for (const match of content.matchAll(INLINE)) {
    const index = match.index ?? 0;
    if (index > cursor) {
      chunks.push({ style: "plain", text: content.slice(cursor, index) });
    }
    if (match[1] !== undefined) {
      chunks.push({ style: "strong", text: match[1] });
    } else {
      chunks.push({ style: "em", text: match[2] });
    }
    cursor = index + match[0].length;
  }
  if (cursor < content.length) {
    chunks.push({ style: "plain", text: content.slice(cursor) });
  }
  return chunks;
}
