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
//! plugin remembers wherever the person put them; this never runs again.

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

/// where the two windows go: (x, y) origins in physical pixels
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Placement {
    pub main: (i32, i32),
    pub deer: (i32, i32),
}

/// the arithmetic, pure so it can be pinned: `work` is the monitor's work
/// area (screen minus dock / taskbar), the sizes are outer window sizes.
pub fn place(work: Rect, main: (i32, i32), deer: (i32, i32), margin: i32) -> Placement {
    let (main_w, main_h) = main;
    let (deer_w, deer_h) = deer;

    // the deer: bottom-right corner, a margin in from both edges, never
    // pushed off the top/left of the work area on a tiny screen
    let deer_x = (work.x + work.w - deer_w - margin).max(work.x);
    let deer_y = (work.y + work.h - deer_h - margin).max(work.y);

    // the main window: centred in the strip to the deer's left when that
    // strip fits it; otherwise flush left, which minimises the overlap
    let strip_w = work.w - deer_w - 2 * margin;
    let main_x = if strip_w >= main_w {
        work.x + (strip_w - main_w) / 2
    } else {
        work.x + margin
    };
    let main_y = work.y + ((work.h - main_h) / 2).max(0);

    Placement {
        main: (main_x, main_y),
        deer: (deer_x, deer_y),
    }
}

/// true when the window-state plugin has nothing remembered yet - i.e.
/// this is the first launch (or the state file was removed on purpose)
pub fn is_first_launch(app: &AppHandle) -> bool {
    match app.path().app_config_dir() {
        Ok(dir) => !dir
            .join(tauri_plugin_window_state::DEFAULT_FILENAME)
            .exists(),
        // no config dir at all is the same answer: nothing remembered
        Err(_) => true,
    }
}

/// place the pair on a first launch. every failure here is cosmetic (the
/// windows simply stay where the config put them), so nothing is fatal.
pub fn apply_first_launch(app: &AppHandle) {
    if !is_first_launch(app) {
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
    let placement = place(
        work,
        (main_size.width as i32, main_size.height as i32),
        (deer_size.width as i32, deer_size.height as i32),
        MARGIN,
    );
    let _ = main.set_position(PhysicalPosition::new(
        placement.main.0,
        placement.main.1,
    ));
    let _ = deer.set_position(PhysicalPosition::new(
        placement.deer.0,
        placement.deer.1,
    ));
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
