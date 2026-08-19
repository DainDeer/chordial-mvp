// where the device token lives between launches (phase 7b: the box).
//
// packaged builds keep it in the OS keychain through the shell's
// credential_* commands (src-tauri/src/credentials.rs) - webview
// localStorage is a plaintext file on disk, and the token is the key to
// the person's whole council. dev builds keep localStorage: an unsigned
// debug binary changes identity every rebuild, and macOS would ask about
// keychain access for each one.
//
// the keychain has no change events, so the deer's cross-window notice
// (link/relink/revoke happen in the main window) rides SESSION_REV_KEY in
// localStorage - a bare counter with no secret in it - which still fires
// the storage event the deer listens on; its slow poll stays as the belt.
//
// a keychain that refuses (denied access, locked) degrades to localStorage
// with a console warning: worse at-rest storage beats a person who cannot
// link their own device at all. two rules keep that degradation honest
// (sol's 7b round - a flaky keychain must never resurrect a revoked
// token): a localStorage token always BEATS the keychain value on load
// (it only exists when the keychain missed a newer write) and moves home;
// and a clear the keychain refused leaves a pending marker so the stuck
// value is never served before the clear finally lands.

import { invoke } from "@tauri-apps/api/core";

export const TOKEN_STORAGE_KEY = "chordial.device_token";
export const SESSION_REV_KEY = "chordial.session_rev";
// a clear the keychain refused is still OWED (sol's 7b round): the value
// in there is revoked, and until the clear lands it must never be served
export const CLEAR_PENDING_KEY = "chordial.keychain_clear_pending";
const DEVICE_KEY = "chordial.device_id";

/** the shell's keychain as the frontend sees it; null = this build keeps
 * localStorage (dev, or a plain browser without the shell) */
export interface SecretStore {
  get(): Promise<string | null>;
  set(value: string): Promise<void>;
  clear(): Promise<void>;
}

const keychain: SecretStore = {
  get: () => invoke<string | null>("credential_get"),
  set: (value: string) => invoke("credential_set", { value }),
  clear: () => invoke("credential_clear"),
};

function activeSecretStore(): SecretStore | null {
  if (!import.meta.env.PROD) return null;
  if (typeof window === "undefined") return null;
  if (!("__TAURI_INTERNALS__" in window)) return null;
  return keychain;
}

/** cross-window "the session changed" signal - a counter, never a secret */
function bumpRev(local: Storage): void {
  const rev = Number(local.getItem(SESSION_REV_KEY) ?? "0") + 1;
  local.setItem(SESSION_REV_KEY, String(rev));
}

// --- the logic, with its stores injected (pendingDone.ts style) so the
// --- migration and fallback paths are pinnable in vitest

export async function loadSessionFrom(
  secrets: SecretStore | null,
  local: Storage,
): Promise<string | null> {
  if (!secrets) return local.getItem(TOKEN_STORAGE_KEY);
  // read once up front: it is also the answer whenever the keychain throws
  const localToken = local.getItem(TOKEN_STORAGE_KEY);
  try {
    if (local.getItem(CLEAR_PENDING_KEY) !== null) {
      // finish a clear the keychain refused earlier - the value in there
      // is revoked and must never come back to life (sol's 7b round). if
      // this throws, the catch serves localStorage/null, never the ghost.
      await secrets.clear();
      local.removeItem(CLEAR_PENDING_KEY);
    }
    // a localStorage token in keychain mode is either the pre-keychain
    // era (keychain empty) or a fallback write from a keychain that
    // refused a set - in which case the keychain holds an OLDER, possibly
    // revoked value. either way localStorage is the newest truth: it
    // wins, and moves home so no plaintext copy lingers.
    if (localToken !== null) {
      await secrets.set(localToken);
      local.removeItem(TOKEN_STORAGE_KEY);
      return localToken;
    }
    return await secrets.get();
  } catch (e) {
    console.warn("keychain unavailable, reading localStorage:", e);
    return localToken;
  }
}

export async function storeSessionIn(
  secrets: SecretStore | null,
  local: Storage,
  token: string,
  deviceId: string,
): Promise<void> {
  if (secrets) {
    try {
      await secrets.set(token);
      // never both: a keychain-held token leaves no localStorage shadow,
      // and the fresh value supersedes any clear still owed for the old
      local.removeItem(TOKEN_STORAGE_KEY);
      local.removeItem(CLEAR_PENDING_KEY);
    } catch (e) {
      console.warn("keychain unavailable, storing in localStorage:", e);
      local.setItem(TOKEN_STORAGE_KEY, token);
    }
  } else {
    local.setItem(TOKEN_STORAGE_KEY, token);
  }
  local.setItem(DEVICE_KEY, deviceId);
  bumpRev(local);
}

export async function clearSessionIn(
  secrets: SecretStore | null,
  local: Storage,
): Promise<void> {
  if (secrets) {
    try {
      await secrets.clear();
      local.removeItem(CLEAR_PENDING_KEY);
    } catch (e) {
      // the revoked token is stuck in the keychain: leave a marker so no
      // future load serves it before the clear finally lands
      console.warn("keychain unavailable while clearing:", e);
      local.setItem(CLEAR_PENDING_KEY, "1");
    }
  }
  local.removeItem(TOKEN_STORAGE_KEY);
  local.removeItem(DEVICE_KEY);
  bumpRev(local);
}

// --- the app-facing api, bound to the real environment

export const loadSession = (): Promise<string | null> =>
  loadSessionFrom(activeSecretStore(), localStorage);

export const storeSession = (token: string, deviceId: string): Promise<void> =>
  storeSessionIn(activeSecretStore(), localStorage, token, deviceId);

export const clearSession = (): Promise<void> =>
  clearSessionIn(activeSecretStore(), localStorage);
