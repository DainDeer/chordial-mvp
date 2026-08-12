import { chunkInline } from "../lib/markup";

/** prose with the council's action beats honored: *em* and **strong** only,
 * everything else literal (chunks are data - react escapes the text, so
 * nothing in a message can inject markup). shared by the room's helper
 * bubbles and the deer's speech bubble. */
export default function InlineContent({ content }: { content: string }) {
  return (
    <>
      {chunkInline(content).map((chunk, i) => {
        if (chunk.style === "em") return <em key={i}>{chunk.text}</em>;
        if (chunk.style === "strong")
          return <strong key={i}>{chunk.text}</strong>;
        return <span key={i}>{chunk.text}</span>;
      })}
    </>
  );
}
