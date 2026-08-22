//! the bundled sidecar's lifecycle (phase 7b: the box).
//!
//! release builds own the sidecar: probe first (a sidecar already answering
//! on the port - a second app launch, or a dev terminal's - is adopted, not
//! fought), spawn the frozen binary with its state pointed at the app data
//! dir, respawn with backoff if it dies, and kill it on quit. debug builds
//! spawn nothing - the four-terminal dev loop keeps the sidecar in its own
//! terminal (`poetry run python -m src.sidecar`), visible and restartable.
//!
//! the onefile binary self-extracts before python starts (~6-8s from spawn
//! to the port binding); nothing here waits on it - the deer window already
//! reconnects with backoff, and the collector treats a dead sidecar as a
//! quiet day.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// a spawn that lived at least this long counts as stable: its eventual
/// death restarts the backoff ladder instead of climbing it
const STABLE_UPTIME: Duration = Duration::from_secs(60);

/// crash-loop backoff, capped: a binary that dies instantly every time
/// costs one respawn every 30s, never a hot loop
const BACKOFF_SECS: [u64; 5] = [1, 2, 5, 15, 30];

/// how often the watchdog re-probes an ADOPTED sidecar (sol's 7b round:
/// adoption must be a watched state, not a terminal one - the adopted
/// process belongs to someone else and can vanish, e.g. the other app
/// instance of a simultaneous launch quitting and killing its child)
const ADOPT_PROBE_SECS: u64 = 15;

pub struct SidecarState {
    child: Mutex<Option<CommandChild>>,
    quitting: AtomicBool,
}

impl SidecarState {
    pub fn new() -> Self {
        Self {
            child: Mutex::new(None),
            quitting: AtomicBool::new(false),
        }
    }
}

fn port() -> u16 {
    std::env::var("SIDECAR_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8485)
}

/// somebody already answering /v1/state on the loopback port IS a sidecar;
/// spawning a second one would just lose the port race and crash-loop
fn already_up(port: u16) -> bool {
    ureq::AgentBuilder::new()
        .timeout(Duration::from_millis(500))
        .build()
        .get(&format!("http://127.0.0.1:{port}/v1/state"))
        .call()
        .is_ok()
}

pub fn spawn(app: &AppHandle) {
    if cfg!(debug_assertions) {
        eprintln!(
            "[sidecar] debug build - not spawning; dev loop runs it: \
             poetry run python -m src.sidecar"
        );
        return;
    }
    spawn_supervised(app.clone(), 0);
}

/// set the quitting flag and kill the child - called from the app's exit
/// event so the sidecar never outlives the shell that owns it
pub fn shutdown(app: &AppHandle) {
    let state = app.state::<SidecarState>();
    state.quitting.store(true, Ordering::SeqCst);
    let child = state.child.lock().unwrap().take();
    if let Some(child) = child {
        let _ = child.kill();
    }
}

/// keep an eye on a sidecar this shell didn't spawn: when it stops
/// answering, take over. taking over re-enters spawn_supervised, which
/// re-probes - so a NEW foreign sidecar appearing in the gap just gets
/// adopted (and watched) again, never fought.
fn watch_adopted(app: AppHandle, port: u16) {
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(ADOPT_PROBE_SECS));
        if app.state::<SidecarState>().quitting.load(Ordering::SeqCst) {
            return;
        }
        if !already_up(port) {
            eprintln!(
                "[sidecar] the adopted sidecar on port {port} went away - \
                 taking over"
            );
            spawn_supervised(app, 0);
            return;
        }
    });
}

fn spawn_supervised(app: AppHandle, attempt: usize) {
    let state = app.state::<SidecarState>();
    if state.quitting.load(Ordering::SeqCst) {
        return;
    }
    let port = port();
    if already_up(port) {
        eprintln!(
            "[sidecar] adopting the sidecar already on port {port} - \
             watching it"
        );
        watch_adopted(app, port);
        return;
    }

    let data_dir = match app.path().app_data_dir() {
        Ok(dir) => dir,
        Err(e) => {
            eprintln!("[sidecar] no app data dir, cannot spawn: {e}");
            return;
        }
    };
    if let Err(e) = std::fs::create_dir_all(&data_dir) {
        eprintln!("[sidecar] cannot create {}: {e}", data_dir.display());
        return;
    }
    let db = data_dir.join("chordial_sidecar.db");

    let command = match app.shell().sidecar("chordial-sidecar") {
        Ok(cmd) => cmd
            .env("SIDECAR_DB", db.to_string_lossy().to_string())
            .env("SIDECAR_PORT", port.to_string())
            // the sidecar follows THIS process out (src/sidecar/parent.py):
            // our kill below only reaches pyinstaller's onefile bootloader,
            // and a force-quit reaches nothing - the python interpreter
            // behind it would outlive us either way (found on the first
            // boxed launch: an orphan holding the port after quit)
            .env("CHORDIAL_SHELL_PID", std::process::id().to_string()),
        Err(e) => {
            eprintln!("[sidecar] bundled binary unresolvable: {e}");
            return;
        }
    };
    let (mut rx, child) = match command.spawn() {
        Ok(pair) => pair,
        Err(e) => {
            eprintln!("[sidecar] spawn failed: {e}");
            return;
        }
    };
    eprintln!("[sidecar] spawned (state in {})", db.display());
    *state.child.lock().unwrap() = Some(child);

    let born = Instant::now();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) | CommandEvent::Stderr(line) => {
                    eprint!("[sidecar] {}", String::from_utf8_lossy(&line));
                }
                CommandEvent::Error(e) => {
                    eprintln!("[sidecar] io error: {e}");
                }
                CommandEvent::Terminated(payload) => {
                    let state = app.state::<SidecarState>();
                    state.child.lock().unwrap().take();
                    if state.quitting.load(Ordering::SeqCst) {
                        return;
                    }
                    // a stable run's death is news, not a climb up the ladder
                    let next = if born.elapsed() >= STABLE_UPTIME {
                        0
                    } else {
                        (attempt + 1).min(BACKOFF_SECS.len() - 1)
                    };
                    let delay = BACKOFF_SECS[next];
                    eprintln!(
                        "[sidecar] exited (code {:?}) - respawn in {delay}s",
                        payload.code
                    );
                    let app = app.clone();
                    std::thread::spawn(move || {
                        std::thread::sleep(Duration::from_secs(delay));
                        spawn_supervised(app, next);
                    });
                    return;
                }
                _ => {}
            }
        }
    });
}
