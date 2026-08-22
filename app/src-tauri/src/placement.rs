//! first-launch window placement: the deer lives at the edge of the
//! screen, never on top of the main window.
//!
//! the tauri config gives both windows a size and no position, which puts
//! both in the centre of the screen - and the deer is always-on-top, so on
//! the very first launch she sat squarely over the link-code field (found
//! on the first boxed run). this module places the pair ONCE, on a launch
//! with no remembered window state: deer in the bottom-right corner of
//! the work area, main window centred in the room to her left (or flush
//! left when the screen is too narrow for both - she's on top and
//! draggable, so an overlap is a nudge away). after that the window-state
//! plugin remembers wherever the person put them.
//!
//! "remembered" is decided PER WINDOW from what the state file actually
//! says (sol's #80 round): a file that is corrupt, empty, or missing a
//! window's entry remembers nothing for that window, so the placement
//! runs for it - a bad file can never pin the centred-overlap layout in
//! place. a window that IS remembered is left exactly where it was; an
//! unremembered main window is placed relative to wherever the deer
//! really is (remembered or just placed), not to an assumed corner.

use tauri::{AppHandle, Manager, PhysicalPosition};

/// breathing room between a window and the work-area edge, in physical
/// pixels
pub const MARGIN: i32 = 16;

/// a rectangle in physical pixels: origin + size
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Rect {
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
}

/// where the two windows go: (x, y) origins in physical pixels (the
/// composed first-launch layout, kept for the tests that pin it - the
/// live path places per window via place_deer / place_main)
#[cfg(test)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Placement {
    pub main: (i32, i32),
    pub deer: (i32, i32),
}

/// the deer: bottom-right corner of the work area, a margin in from both
/// edges, never pushed off the top/left on a tiny screen
pub fn place_deer(work: Rect, deer: (i32, i32), margin: i32) -> (i32, i32) {
    let (deer_w, deer_h) = deer;
    (
        (work.x + work.w - deer_w - margin).max(work.x),
        (work.y + work.h - deer_h - margin).max(work.y),
    )
}

/// the main window, given where the deer actually is: centred in the
/// wider of the two strips beside her (left or right) when that strip
/// fits it; otherwise flush left, which minimises the overlap
pub fn place_main(work: Rect, main: (i32, i32), deer: Rect, margin: i32) -> (i32, i32) {
    let (main_w, main_h) = main;
    let left_w = deer.x - work.x - margin;
    let right_x = deer.x + deer.w + margin;
    let right_w = work.x + work.w - right_x;
    let main_x = if left_w >= right_w && left_w >= main_w {
        work.x + (left_w - main_w) / 2
    } else if right_w > left_w && right_w >= main_w {
        right_x + (right_w - main_w) / 2
    } else {
        work.x + margin
    };
    let main_y = work.y + ((work.h - main_h) / 2).max(0);
    (main_x, main_y)
}

/// the whole first-launch layout, pure so it can be pinned: `work` is the
/// monitor's work area (screen minus dock / taskbar), the sizes are outer
/// window sizes
#[cfg(test)]
pub fn place(work: Rect, main: (i32, i32), deer: (i32, i32), margin: i32) -> Placement {
    let deer_pos = place_deer(work, deer, margin);
    let deer_rect = Rect { x: deer_pos.0, y: deer_pos.1, w: deer.0, h: deer.1 };
    Placement {
        main: place_main(work, main, deer_rect, margin),
        deer: deer_pos,
    }
}

/// does the window-state file remember a position for this window? only
/// a parseable object with an entry for the label carrying numeric x and
/// y counts - anything else (no file, corrupt json, the wrong shape, the
/// label missing, an entry without a position) is "not remembered", and
/// the placement runs for that window.
pub fn window_remembered(state_json: Option<&str>, label: &str) -> bool {
    let Some(text) = state_json else {
        return false;
    };
    let Ok(value) = serde_json::from_str::<serde_json::Value>(text) else {
        return false;
    };
    let Some(entry) = value.get(label) else {
        return false;
    };
    entry.get("x").and_then(|v| v.as_i64()).is_some()
        && entry.get("y").and_then(|v| v.as_i64()).is_some()
}

/// the state file's text, if there is one (None = nothing remembered)
fn state_file_text(app: &AppHandle) -> Option<String> {
    let dir = app.path().app_config_dir().ok()?;
    std::fs::read_to_string(dir.join(tauri_plugin_window_state::DEFAULT_FILENAME)).ok()
}

/// place whatever the state file doesn't remember. every failure here is
/// cosmetic (the windows simply stay where the config put them), so
/// nothing is fatal.
pub fn apply_first_launch(app: &AppHandle) {
    let text = state_file_text(app);
    let main_remembered = window_remembered(text.as_deref(), "main");
    let deer_remembered = window_remembered(text.as_deref(), "deer");
    if main_remembered && deer_remembered {
        return;
    }
    let (Some(main), Some(deer)) = (
        app.get_webview_window("main"),
        app.get_webview_window("deer"),
    ) else {
        return;
    };
    let Ok(Some(monitor)) = main.primary_monitor() else {
        return;
    };
    let area = monitor.work_area();
    let work = Rect {
        x: area.position.x,
        y: area.position.y,
        w: area.size.width as i32,
        h: area.size.height as i32,
    };
    let (Ok(main_size), Ok(deer_size)) = (main.outer_size(), deer.outer_size()) else {
        return;
    };
    let deer_dims = (deer_size.width as i32, deer_size.height as i32);

    // the deer first: her real rectangle is what main is placed against
    let deer_rect = if deer_remembered {
        let Ok(pos) = deer.outer_position() else {
            return;
        };
        Rect { x: pos.x, y: pos.y, w: deer_dims.0, h: deer_dims.1 }
    } else {
        let (x, y) = place_deer(work, deer_dims, MARGIN);
        let _ = deer.set_position(PhysicalPosition::new(x, y));
        Rect { x, y, w: deer_dims.0, h: deer_dims.1 }
    };

    if !main_remembered {
        let (x, y) = place_main(
            work,
            (main_size.width as i32, main_size.height as i32),
            deer_rect,
            MARGIN,
        );
        let _ = main.set_position(PhysicalPosition::new(x, y));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // a 13" laptop at 2x: 2880x1800 minus the menu bar + dock
    const LAPTOP: Rect = Rect { x: 0, y: 50, w: 2880, h: 1650 };
    const MAIN: (i32, i32) = (2200, 1440); // 1100x720 logical @2x
    const DEER: (i32, i32) = (540, 1000); // 270x500 logical @2x

    #[test]
    fn deer_sits_in_the_bottom_right_corner() {
        let p = place(LAPTOP, MAIN, DEER, MARGIN);
        assert_eq!(p.deer, (2880 - 540 - 16, 50 + 1650 - 1000 - 16));
    }

    #[test]
    fn main_window_never_under_the_deer_on_a_wide_screen() {
        // a 27" at 2x: plenty of room - main is centred in the strip to
        // the deer's left and the two rectangles don't touch
        let wide = Rect { x: 0, y: 50, w: 5120, h: 2830 };
        let p = place(wide, MAIN, DEER, MARGIN);
        let strip = 5120 - 540 - 2 * 16;
        assert_eq!(p.main.0, (strip - 2200) / 2);
        assert!(p.main.0 + MAIN.0 <= p.deer.0, "main overlaps the deer");
        assert_eq!(p.main.1, 50 + (2830 - 1440) / 2);
    }

    #[test]
    fn laptop_fits_both_side_by_side() {
        // the 13" at 2x holds 2200 + 540 + margins (2772 < 2880): main is
        // centred in the strip and clear of the deer
        let p = place(LAPTOP, MAIN, DEER, MARGIN);
        let strip = 2880 - 540 - 2 * 16;
        assert_eq!(p.main.0, (strip - 2200) / 2);
        assert!(p.main.0 + MAIN.0 <= p.deer.0, "main overlaps the deer");
    }

    #[test]
    fn narrow_screen_goes_flush_left_to_minimise_overlap() {
        // a 1280x800 external at 1x can't hold 1100 + 270 + margins side
        // by side, so main hugs the left edge and the overlap is the deer's
        // problem (she's on top and draggable)
        let small = Rect { x: 0, y: 25, w: 1280, h: 775 };
        let p = place(small, (1100, 720), (270, 500), MARGIN);
        assert_eq!(p.main.0, MARGIN);
        assert_eq!(p.main.1, 25 + (775 - 720) / 2);
        assert_eq!(p.deer, (1280 - 270 - 16, 25 + 775 - 500 - 16));
    }

    #[test]
    fn tiny_screen_never_pushes_windows_off_the_top_left() {
        let tiny = Rect { x: 0, y: 0, w: 400, h: 300 };
        let p = place(tiny, MAIN, DEER, MARGIN);
        assert_eq!(p.deer, (0, 0));
        assert_eq!(p.main, (MARGIN, 0));
    }

    #[test]
    fn main_goes_to_the_wider_side_of_a_remembered_deer() {
        // the deer was parked bottom-LEFT by the person; a fresh main
        // window (state entry missing) centres in the room to her right
        let wide = Rect { x: 0, y: 50, w: 5120, h: 2830 };
        let deer = Rect { x: 16, y: 1864, w: 540, h: 1000 };
        let (x, _) = place_main(wide, MAIN, deer, MARGIN);
        let right_x = 16 + 540 + 16;
        let right_w = 5120 - right_x;
        assert_eq!(x, right_x + (right_w - 2200) / 2);
        assert!(x >= deer.x + deer.w, "main overlaps the parked deer");
    }

    // --- what counts as remembered (sol's #80 round) ---

    #[test]
    fn no_file_remembers_nothing() {
        assert!(!window_remembered(None, "deer"));
    }

    #[test]
    fn corrupt_or_wrong_shape_remembers_nothing() {
        for bad in ["", "{", "[]", "null", "42", "\"deer\""] {
            assert!(!window_remembered(Some(bad), "deer"), "{bad:?}");
            assert!(!window_remembered(Some(bad), "main"), "{bad:?}");
        }
    }

    #[test]
    fn remembered_is_per_window() {
        // main saved, deer missing: the deer is placed, main is left alone
        let only_main = r#"{"main":{"width":2200,"height":1440,"x":100,"y":120,
            "prev_x":0,"prev_y":0,"maximized":false,"visible":true,
            "decorated":true,"fullscreen":false}}"#;
        assert!(window_remembered(Some(only_main), "main"));
        assert!(!window_remembered(Some(only_main), "deer"));
    }

    #[test]
    fn an_entry_without_a_position_is_not_remembered() {
        let no_pos = r#"{"deer":{"width":540,"height":1000}}"#;
        assert!(!window_remembered(Some(no_pos), "deer"));
        let half = r#"{"deer":{"x":10}}"#;
        assert!(!window_remembered(Some(half), "deer"));
    }

    #[test]
    fn both_remembered_is_the_steady_state() {
        let both = r#"{"main":{"x":1,"y":2},"deer":{"x":3,"y":4}}"#;
        assert!(window_remembered(Some(both), "main"));
        assert!(window_remembered(Some(both), "deer"));
    }

    #[test]
    fn secondary_monitor_origin_is_honoured() {
        // a work area that doesn't start at 0,0 (the primary to the right
        // of another display, or a taskbar on the left)
        let shifted = Rect { x: 1920, y: 100, w: 5120, h: 2830 };
        let p = place(shifted, MAIN, DEER, MARGIN);
        assert_eq!(p.deer.0, 1920 + 5120 - 540 - 16);
        assert_eq!(p.deer.1, 100 + 2830 - 1000 - 16);
        assert!(p.main.0 >= 1920);
    }
}
