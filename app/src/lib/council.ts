// each council member gets a hue the ui leans on for avatar rings and name
// chips - authored here (they're part of the visual identity, not server
// data). tuned to read on the clearing's light pink-lavender ground; the
// fallback is the site's deep blush so nothing ever renders unstyled.

export const COUNCIL_HUES: Record<string, string> = {
  vel: "#e98fbb", // the site's own deep blush - the deer IS the brand
  pip: "#d97a4a", // acorn-cheeked terracotta
  skip: "#5cb374", // trailside sage, deepened for daylight
  remy: "#a081d8", // twilight lavender for a refined raccoon
  mabel: "#c9628f", // warm rose, den-shaped
  juniper: "#3fa89b", // juniper teal
  edwin: "#6a87ad", // ledger-ink slate
};

export const memberHue = (id: string): string =>
  COUNCIL_HUES[id] ?? "#e98fbb";
