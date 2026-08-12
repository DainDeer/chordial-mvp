import { describe, expect, it } from "vitest";
import { chunkInline } from "./markup";

describe("chunkInline", () => {
  it("renders action beats as emphasis", () => {
    expect(chunkInline("*ears perk* hi!!")).toEqual([
      { style: "em", text: "ears perk" },
      { style: "plain", text: " hi!!" },
    ]);
  });

  it("supports strong and em together", () => {
    expect(chunkInline("this is **firm mode**, *gently*")).toEqual([
      { style: "plain", text: "this is " },
      { style: "strong", text: "firm mode" },
      { style: "plain", text: ", " },
      { style: "em", text: "gently" },
    ]);
  });

  it("leaves unmatched asterisks literal", () => {
    expect(chunkInline("a * b and 2 * 3 * 4")).toEqual([
      { style: "plain", text: "a * b and 2 * 3 * 4" },
    ]);
  });

  it("passes plain text through as one chunk", () => {
    expect(chunkInline("nothing fancy here")).toEqual([
      { style: "plain", text: "nothing fancy here" },
    ]);
  });

  it("keeps line breaks inside plain chunks (pre-wrap renders them)", () => {
    expect(chunkInline("one\n\ntwo *three*")).toEqual([
      { style: "plain", text: "one\n\ntwo " },
      { style: "em", text: "three" },
    ]);
  });

  it("never emits markup characters as structure - output is data only", () => {
    const chunks = chunkInline("*a* **b** <script>x</script>");
    expect(chunks.map((c) => c.text).join("")).toBe("a b <script>x</script>");
  });
});
