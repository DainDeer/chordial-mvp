// where the device token lives between launches. localStorage is the
// dev-mode answer (tauri's webview storage is per-app and local-only);
// phase 7 packaging can graduate this to the stronghold/keychain plugin.

const TOKEN_KEY = "chordial.device_token";
const DEVICE_KEY = "chordial.device_id";

export function storedToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function storeSession(token: string, deviceId: string): void {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(DEVICE_KEY, deviceId);
}

export function clearSession(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(DEVICE_KEY);
}
