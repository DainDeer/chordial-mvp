// each council member gets a hue the ui leans on for avatar rings and name
// chips - authored here (they're part of the visual identity, not server
// data). unknown ids (a future member, a renamed deployment) fall back to
// the ember accent so nothing ever renders unstyled.

export const COUNCIL_HUES: Record<string, string> = {
  vel: "#e8a64d", // candle amber - the hearth itself
  pip: "#e0784f", // acorn-cheeked terracotta
  skip: "#9dbb82", // trailside moss
  remy: "#c9a2d8", // twilight lavender for a refined raccoon
  mabel: "#d98ba3", // warm rose, den-shaped
  juniper: "#6fbcb0", // juniper teal, obviously
  edwin: "#8aa3c4", // ledger-ink slate
};

export const memberHue = (id: string): string =>
  COUNCIL_HUES[id] ?? "#e8a64d";
