//! the activity collectors (docs/ROOMS_DESIGN.md §5): frontmost bundle id
//! and idle seconds, on a 2-second poll, delivered to the local sidecar.
//!
//! THIS FILE IS THE PRIVACY BOUNDARY. exactly two signals ever leave it -
//! a sanitized bundle identifier and a number of idle seconds - and both
//! only travel to 127.0.0.1. window titles, document names, urls, and
//! screen content are structurally absent: nothing here ever asks for them,
//! so no later bug can leak what was never collected.

use std::{thread, time::Duration};

const POLL_SECONDS: u64 = 2;
/// resend cadence when nothing changed - the sidecar wants a heartbeat so
/// idle keeps counting even when the person is perfectly still
const HEARTBEAT_TICKS: u32 = 5;

#[cfg(target_os = "macos")]
fn frontmost_bundle_id() -> Option<String> {
    use objc2_app_kit::NSWorkspace;
    let workspace = NSWorkspace::sharedWorkspace();
    let app = workspace.frontmostApplication()?;
    let bundle_id = app.bundleIdentifier()?;
    Some(bundle_id.to_string())
}

#[cfg(not(target_os = "macos"))]
fn frontmost_bundle_id() -> Option<String> {
    None
}

#[cfg(target_os = "macos")]
fn idle_seconds() -> f64 {
    // CGEventSourceSecondsSinceLastEventType with kCGAnyInputEventType:
    // seconds since the person last touched anything. no event tap, no
    // accessibility permission - it reads a counter, never the events.
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGEventSourceSecondsSinceLastEventType(
            state: u32,
            event_type: u32,
        ) -> f64;
    }
    const COMBINED_SESSION_STATE: u32 = 0;
    const ANY_INPUT_EVENT_TYPE: u32 = u32::MAX; // kCGAnyInputEventType
    unsafe {
        CGEventSourceSecondsSinceLastEventType(
            COMBINED_SESSION_STATE,
            ANY_INPUT_EVENT_TYPE,
        )
    }
}

#[cfg(not(target_os = "macos"))]
fn idle_seconds() -> f64 {
    0.0
}

/// the sanitizer: a bundle id is reverse-dns ascii or it is nothing.
/// anything shaped differently (or absurdly long) is dropped at the
/// boundary rather than forwarded and filtered later.
fn sanitize(bundle_id: Option<String>) -> Option<String> {
    let id = bundle_id?;
    if id.is_empty() || id.len() > 200 {
        return None;
    }
    if id
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '-' || c == '_')
    {
        Some(id)
    } else {
        None
    }
}

pub fn spawn() {
    let port: u16 = std::env::var("SIDECAR_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8485);
    let endpoint = format!("http://127.0.0.1:{port}/v1/activity");

    thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout(Duration::from_secs(3))
            .build();
        let mut last_sent: Option<String> = None;
        let mut ticks_since_send: u32 = 0;

        loop {
            thread::sleep(Duration::from_secs(POLL_SECONDS));
            let bundle = sanitize(frontmost_bundle_id());
            ticks_since_send += 1;
            let changed = bundle != last_sent;
            if !changed && ticks_since_send < HEARTBEAT_TICKS {
                continue;
            }
            let payload = serde_json::json!({
                "bundle_id": bundle,
                "idle_seconds": idle_seconds(),
            });
            // a dead sidecar is a quiet day, not an error worth logging
            // at a 2s cadence
            if agent.post(&endpoint).send_json(payload).is_ok() {
                last_sent = bundle;
                ticks_since_send = 0;
            }
        }
    });
}
