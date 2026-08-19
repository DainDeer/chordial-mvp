//! the device token's at-rest home (phase 7b: the box).
//!
//! packaged builds keep the bearer token in the OS keychain (macOS
//! Keychain, Windows Credential Manager) instead of webview localStorage -
//! webview storage is a plaintext file on disk; the keychain is the
//! platform's own answer to exactly this. the frontend reaches these
//! through invoke() and keeps localStorage for dev builds, where an
//! unsigned debug binary changes identity every rebuild and macOS would
//! ask permission for each one.
//!
//! deliberately the whole surface: get, set, clear. one service, one
//! account, one secret - the token. the deer's cross-window "the token
//! changed" signal stays in localStorage as a REV COUNTER with no secret
//! in it (see app/src/lib/session.ts).

/// matches the app identifier - the keychain item is unambiguously ours
const SERVICE: &str = "app.chordial.desktop";
const ACCOUNT: &str = "device_token";

#[cfg(any(target_os = "macos", target_os = "windows"))]
fn entry() -> Result<keyring::Entry, String> {
    keyring::Entry::new(SERVICE, ACCOUNT).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn credential_get() -> Result<Option<String>, String> {
    #[cfg(any(target_os = "macos", target_os = "windows"))]
    {
        match entry()?.get_password() {
            Ok(secret) => Ok(Some(secret)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(e) => Err(e.to_string()),
        }
    }
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    {
        Err("no keychain backend on this platform".to_string())
    }
}

#[tauri::command]
pub fn credential_set(value: String) -> Result<(), String> {
    #[cfg(any(target_os = "macos", target_os = "windows"))]
    {
        entry()?.set_password(&value).map_err(|e| e.to_string())
    }
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    {
        let _ = value;
        Err("no keychain backend on this platform".to_string())
    }
}

#[tauri::command]
pub fn credential_clear() -> Result<(), String> {
    #[cfg(any(target_os = "macos", target_os = "windows"))]
    {
        match entry()?.delete_credential() {
            Ok(()) => Ok(()),
            // clearing what isn't there is the goal already met
            Err(keyring::Error::NoEntry) => Ok(()),
            Err(e) => Err(e.to_string()),
        }
    }
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    {
        Err("no keychain backend on this platform".to_string())
    }
}
