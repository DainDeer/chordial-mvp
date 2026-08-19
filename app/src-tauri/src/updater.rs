//! the shell keeps itself fresh (phase 7b: the box).
//!
//! the update feed lives on the chordial server itself (/app/updates/ -
//! latest.json plus signed bundles), so the app polls the one origin it
//! already trusts. bundles are minisign-signed at build time; the pubkey
//! pinned in tauri.conf.json refuses anything else, so a compromised feed
//! can withhold updates but never forge one.
//!
//! two entry points, one flow: a quiet check shortly after launch (silent
//! when current or unreachable), and the tray's "check for updates" (which
//! answers either way). both ask before installing - the update is never
//! the first thing that happens to someone's morning.

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use tauri::AppHandle;
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
use tauri_plugin_updater::UpdaterExt;

/// launch check delay: let the windows open and the sidecar spawn first
const LAUNCH_CHECK_DELAY: Duration = Duration::from_secs(15);

/// exactly one check/install at a time (sol's 7b round): the launch check
/// and impatient tray clicks must never run two installers over the same
/// .app bundle
static CHECK_IN_FLIGHT: AtomicBool = AtomicBool::new(false);

/// releases the in-flight flag on EVERY exit path, early returns included
struct InFlightGuard;

impl Drop for InFlightGuard {
    fn drop(&mut self) {
        CHECK_IN_FLIGHT.store(false, Ordering::SeqCst);
    }
}

pub fn check_on_launch(app: &AppHandle) {
    if cfg!(debug_assertions) {
        eprintln!("[updater] debug build - updater off");
        return;
    }
    let app = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(LAUNCH_CHECK_DELAY);
        check_flow(app, false);
    });
}

pub fn check_interactive(app: &AppHandle) {
    if cfg!(debug_assertions) {
        eprintln!("[updater] debug build - updater off");
        return;
    }
    let app = app.clone();
    std::thread::spawn(move || check_flow(app, true));
}

/// the whole conversation runs on its own thread: blocking dialogs block
/// only this thread, and the async updater calls ride block_on
fn check_flow(app: AppHandle, interactive: bool) {
    if CHECK_IN_FLIGHT
        .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
        .is_err()
    {
        eprintln!("[updater] a check is already running - not starting another");
        if interactive {
            app.dialog()
                .message("already checking for updates - one moment.")
                .title("chordial")
                .blocking_show();
        }
        return;
    }
    let _in_flight = InFlightGuard;

    let updater = match app.updater() {
        Ok(u) => u,
        Err(e) => {
            eprintln!("[updater] not configured: {e}");
            return;
        }
    };
    let update = match tauri::async_runtime::block_on(updater.check()) {
        Ok(Some(update)) => update,
        Ok(None) => {
            if interactive {
                app.dialog()
                    .message("you're on the newest chordial already.")
                    .title("chordial")
                    .blocking_show();
            }
            return;
        }
        Err(e) => {
            // an unreachable feed is a shrug on the quiet path - the next
            // launch tries again - and an honest answer on the loud one
            eprintln!("[updater] check failed: {e}");
            if interactive {
                app.dialog()
                    .message(format!("couldn't reach the update feed: {e}"))
                    .title("chordial")
                    .blocking_show();
            }
            return;
        }
    };

    let wanted = app
        .dialog()
        .message(format!(
            "chordial {} is ready (you have {}).\n\ninstall now? it \
             downloads in the background and applies on restart.",
            update.version, update.current_version
        ))
        .title("a new chordial")
        .buttons(MessageDialogButtons::OkCancelCustom(
            "install".to_string(),
            "not now".to_string(),
        ))
        .blocking_show();
    if !wanted {
        return;
    }

    match tauri::async_runtime::block_on(
        update.download_and_install(|_, _| {}, || {}),
    ) {
        Ok(()) => {
            let restart = app
                .dialog()
                .message("installed. restart chordial to finish?")
                .title("a new chordial")
                .buttons(MessageDialogButtons::OkCancelCustom(
                    "restart".to_string(),
                    "later".to_string(),
                ))
                .blocking_show();
            if restart {
                app.restart();
            }
        }
        Err(e) => {
            eprintln!("[updater] install failed: {e}");
            app.dialog()
                .message(format!(
                    "the update couldn't be installed: {e}\n\nnothing \
                     changed - chordial keeps running as it is."
                ))
                .title("chordial")
                .blocking_show();
        }
    }
}
