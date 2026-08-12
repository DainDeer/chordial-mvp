// the offline-finish ledger: task ids whose local run finished (the deer
// already celebrated - the WORK happened) but whose canonical done-mutation
// hasn't reached the server yet. persisted so a crash or restart never
// forgets a finish; the deer window retries until the server confirms and
// renders these as "syncing" rather than open-and-clickable.
//
// storage is injected (window.localStorage in the app) so the logic stays
// testable without a dom.

const KEY = "chordial.pending_done";

export function listPendingDone(storage: Storage): number[] {
  try {
    const raw = storage.getItem(KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed)
      ? parsed.filter((n): n is number => typeof n === "number")
      : [];
  } catch {
    return [];
  }
}

export function addPendingDone(storage: Storage, taskId: number): number[] {
  const current = listPendingDone(storage);
  const next = current.includes(taskId) ? current : [...current, taskId];
  storage.setItem(KEY, JSON.stringify(next));
  return next;
}

export function removePendingDone(storage: Storage, taskId: number): number[] {
  const next = listPendingDone(storage).filter((id) => id !== taskId);
  storage.setItem(KEY, JSON.stringify(next));
  return next;
}
