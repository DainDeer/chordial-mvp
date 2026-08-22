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
/// instance of a simultaneous launch quitting and killing its child).
/// a loopback GET every few seconds is free; the price of a long interval
/// is a deer-less gap after the other shell leaves (sol's #80 round).
const ADOPT_PROBE_SECS: u64 = 5;

/// how long to wait for a DEPARTING sidecar (one whose shell is already
/// gone) to release the port before spawning our own: its parent watch
/// polls every 2s and then shuts down cleanly, so this is generous
const DEPARTURE_GRACE: Duration = Duration::from_secs(8);

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

/// what answers on the loopback port
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Probe {
    /// nothing answering: the port is ours to take
    Down,
    /// a sidecar is up; `shell_pid` is the shell it follows (None for a
    /// dev-terminal sidecar, which follows nobody)
    Up { shell_pid: Option<u32> },
}

/// what to do about it
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Decision {
    /// spawn our own
    Spawn,
    /// somebody else's live sidecar: adopt it and watch it
    Adopt,
    /// a sidecar whose shell is already gone - it is mid-departure (an
    /// update restart: the old shell quit, its sidecar has up to 2s to
    /// notice). adopting a ghost would mean a deer-less gap of a watchdog
    /// interval plus a boot; wait for it to leave the port, then spawn.
    WaitThenSpawn,
}

/// the adopt-or-spawn call, pure so it can be pinned: `shell_alive`
/// answers whether the pid a found sidecar follows still exists.
pub(crate) fn decide(
    probe: Probe,
    our_pid: u32,
    shell_alive: impl Fn(u32) -> bool,
) -> Decision {
    match probe {
        Probe::Down => Decision::Spawn,
        // a sidecar following THIS shell (a re-probe after our own child's
        // death raced its port release) or following nobody: adopt
        Probe::Up { shell_pid: None } => Decision::Adopt,
        Probe::Up { shell_pid: Some(pid) } if pid == our_pid => Decision::Adopt,
        Probe::Up { shell_pid: Some(pid) } => {
            if shell_alive(pid) {
                Decision::Adopt
            } else {
                Decision::WaitThenSpawn
            }
        }
    }
}

/// somebody already answering /v1/state on the loopback port IS a sidecar;
/// spawning a second one would just lose the port race and crash-loop.
/// the state payload carries `shell_pid` (src/sidecar/server.py) - absent
/// or unparseable reads as "follows nobody", the safe (adopt) reading.
fn probe(port: u16) -> Probe {
    let response = ureq::AgentBuilder::new()
        .timeout(Duration::from_millis(500))
        .build()
        .get(&format!("http://127.0.0.1:{port}/v1/state"))
        .call();
    match response {
        Err(_) => Probe::Down,
        Ok(resp) => {
            let shell_pid = resp
                .into_json::<serde_json::Value>()
                .ok()
                .and_then(|v| v.get("shell_pid").and_then(|p| p.as_u64()))
                .map(|p| p as u32);
            Probe::Up { shell_pid }
        }
    }
}

fn already_up(port: u16) -> bool {
    probe(port) != Probe::Down
}

/// is there a live process with this pid? a liveness probe (the same
/// shape as the sidecar's own parent watch), not an identity check
#[cfg(unix)]
fn shell_alive(pid: u32) -> bool {
    // kill(pid, 0): 0 = exists and ours to signal; EPERM = exists, not
    // ours; ESRCH = gone
    let rc = unsafe { libc::kill(pid as libc::pid_t, 0) };
    if rc == 0 {
        return true;
    }
    std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

#[cfg(windows)]
fn shell_alive(pid: u32) -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, WAIT_OBJECT_0};
    use windows_sys::Win32::System::Threading::{
        OpenProcess, WaitForSingleObject, PROCESS_SYNCHRONIZE,
    };
    unsafe {
        // PROCESS_SYNCHRONIZE (0x00100000): the one right that lets us
        // wait on the process without touching it
        let handle = OpenProcess(PROCESS_SYNCHRONIZE, 0, pid);
        if handle.is_null() {
            return false;
        }
        // signalled = exited (a handle to an exited process still opens)
        let alive = WaitForSingleObject(handle, 0) != WAIT_OBJECT_0;
        CloseHandle(handle);
        alive
    }
}

/// wait for a departing sidecar to release the port (poll, bounded);
/// true when it left, false when the grace ran out
fn wait_for_departure(port: u16, grace: Duration) -> bool {
    let deadline = Instant::now() + grace;
    while Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(250));
        if probe(port) == Probe::Down {
            return true;
        }
    }
    false
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
    match decide(probe(port), std::process::id(), shell_alive) {
        Decision::Spawn => {}
        Decision::Adopt => {
            eprintln!(
                "[sidecar] adopting the sidecar already on port {port} - \
                 watching it"
            );
            watch_adopted(app, port);
            return;
        }
        Decision::WaitThenSpawn => {
            eprintln!(
                "[sidecar] the sidecar on port {port} follows a shell that \
                 is gone - waiting for it to leave, then spawning our own"
            );
            if !wait_for_departure(port, DEPARTURE_GRACE) {
                // it didn't leave: treat it as somebody else's after all
                // (the watchdog takes over the moment it finally goes)
                eprintln!(
                    "[sidecar] still on port {port} after the grace - \
                     adopting and watching it"
                );
                watch_adopted(app, port);
                return;
            }
        }
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

#[cfg(test)]
mod tests {
    use super::*;

    const US: u32 = 1000;

    #[test]
    fn nothing_on_the_port_means_spawn() {
        assert_eq!(decide(Probe::Down, US, |_| true), Decision::Spawn);
    }

    #[test]
    fn a_dev_terminal_sidecar_is_adopted() {
        // follows nobody: a `python -m src.sidecar` in a terminal
        assert_eq!(
            decide(Probe::Up { shell_pid: None }, US, |_| false),
            Decision::Adopt
        );
    }

    #[test]
    fn another_live_shells_sidecar_is_adopted() {
        // a second app instance launched at the same time
        assert_eq!(
            decide(Probe::Up { shell_pid: Some(77) }, US, |pid| pid == 77),
            Decision::Adopt
        );
    }

    #[test]
    fn a_departing_sidecar_is_waited_out_not_adopted() {
        // sol's #80 round: an update restart - the old shell is gone, its
        // sidecar has up to 2s left. adopting it would cost a watchdog
        // interval + a boot of deer-less time; wait for it instead
        assert_eq!(
            decide(Probe::Up { shell_pid: Some(77) }, US, |_| false),
            Decision::WaitThenSpawn
        );
    }

    #[test]
    fn our_own_sidecar_is_adopted_even_if_we_look_dead_to_the_probe() {
        // a sidecar following THIS pid answering a re-probe: ours
        assert_eq!(
            decide(Probe::Up { shell_pid: Some(US) }, US, |_| false),
            Decision::Adopt
        );
    }

    #[test]
    fn our_own_pid_is_alive() {
        assert!(shell_alive(std::process::id()));
    }
}
