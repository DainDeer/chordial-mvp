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
// link their own device at all.

import { invoke } from "@tauri-apps/api/core";

export const TOKEN_STORAGE_KEY = "chordial.device_token";
export const SESSION_REV_KEY = "chordial.session_rev";
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
  try {
    const held = await secrets.get();
    if (held !== null) return held;
    // graduation day: a token from the localStorage era moves into the
    // keychain and leaves no plaintext copy behind
    const legacy = local.getItem(TOKEN_STORAGE_KEY);
    if (legacy !== null) {
      await secrets.set(legacy);
      local.removeItem(TOKEN_STORAGE_KEY);
    }
    return legacy;
  } catch (e) {
    console.warn("keychain unavailable, reading localStorage:", e);
    return local.getItem(TOKEN_STORAGE_KEY);
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
      // never both: a keychain-held token leaves no localStorage shadow
      local.removeItem(TOKEN_STORAGE_KEY);
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
    } catch (e) {
      console.warn("keychain unavailable while clearing:", e);
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
