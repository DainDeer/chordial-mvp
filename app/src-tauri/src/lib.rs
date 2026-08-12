mod collector;

use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager,
};

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            // the collectors: frontmost bundle id + idle, 2s poll, loopback
            // to the sidecar. sanitized inside collector.rs - the boundary.
            collector::spawn();

            // the tray: chordial lives in the corner of the day, so the
            // deer can be tucked away and called back without the dock
            let toggle = MenuItem::with_id(
                app, "toggle-deer", "show / hide the deer", true,
                None::<&str>,
            )?;
            let quit = MenuItem::with_id(
                app, "quit", "quit chordial", true, None::<&str>,
            )?;
            let menu = Menu::with_items(app, &[&toggle, &quit])?;
            let mut tray = TrayIconBuilder::new().menu(&menu).on_menu_event(
                |app, event| match event.id.as_ref() {
                    "toggle-deer" => {
                        if let Some(deer) = app.get_webview_window("deer") {
                            if deer.is_visible().unwrap_or(false) {
                                let _ = deer.hide();
                            } else {
                                let _ = deer.show();
                                let _ = deer.set_focus();
                            }
                        }
                    }
                    "quit" => app.exit(0),
                    _ => {}
                },
            );
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.build(app)?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
