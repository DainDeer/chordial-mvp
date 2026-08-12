// what the room is doing while a reply is on its way. cozy, occasionally
// deeply unserious - the waiting line is a tiny inhabitant of the world,
// not a spinner. picked at random, never the same phrase twice in a row.

export const STIRRINGS: string[] = [
  "the room is stirring",
  "postulating",
  "smelling the flowers",
  "hydrating",
  "running around in circles",
  "consulting the council",
  "rummaging in the acorn drawer",
  "unloafing",
  "checking the ledger",
  "perking ears",
  "following a butterfly",
  "convening a tiny meeting",
  "sniffing the breeze",
  "filing a form with the municipality",
  "rearranging the den",
  "taking a lap around the meadow",
  "looking for the good pen",
  "brewing something warm",
  "gathering thoughts like berries",
  "doing one small stretch",
  "counting sunbeams",
  "practicing a little speech",
  "tidying the burrow",
  "warming up the kitchen",
];

/** a random stirring, never `current` (so the rotation always visibly moves) */
export function pickStirring(current?: string): string {
  const pool = STIRRINGS.filter((s) => s !== current);
  return pool[Math.floor(Math.random() * pool.length)];
}
