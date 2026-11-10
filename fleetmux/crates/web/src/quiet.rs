//! Quiet window (`FLEETMUX_WEB_QUIET=1`): for test runs on a desktop someone is using. The window is still real —
//! WebKit loads, renders and answers `web.sock` as usual — but it never shows, never takes the focus and never
//! puts an icon in the Dock. Without it every window test popped a window over whatever the person was doing and
//! took the keyboard (about 20 per gate run). The tests set it on every window they start; people never do.
//!
//! macOS: the app runs as an accessory and does not activate at launch; the window is built hidden, made fully
//! transparent and click-through, then ordered in (so WebKit treats it as on screen); `open` does not focus it.
//! Linux: only the focus is skipped (CI runs under Xvfb, nobody sees it).
use tao::event_loop::EventLoop;
use tao::window::{Window, WindowBuilder};

/// `FLEETMUX_WEB_QUIET=1`.
pub fn on() -> bool {
    std::env::var("FLEETMUX_WEB_QUIET").as_deref() == Ok("1")
}

/// Before `run`: no activation at launch, no Dock icon.
pub fn event_loop<T: 'static>(event_loop: &mut EventLoop<T>) {
    #[cfg(target_os = "macos")]
    {
        use tao::platform::macos::{ActivationPolicy, EventLoopExtMacOS};
        event_loop.set_activation_policy(ActivationPolicy::Accessory);
        event_loop.set_activate_ignoring_other_apps(false);
        event_loop.set_dock_visibility(false);
    }
    #[cfg(not(target_os = "macos"))]
    let _ = event_loop;
}

/// The builder: hidden until [`window`] has made it transparent (macOS shows a window at build time otherwise).
pub fn builder(builder: WindowBuilder) -> WindowBuilder {
    if cfg!(target_os = "macos") {
        builder.with_visible(false)
    } else {
        builder
    }
}

/// Right after build: transparent, click-through, then ordered in.
pub fn window(window: &Window) {
    #[cfg(target_os = "macos")]
    {
        use objc2::msg_send;
        use objc2::runtime::AnyObject;
        use tao::platform::macos::WindowExtMacOS;
        let ns_window = window.ns_window() as *const AnyObject;
        // SAFETY: tao's live NSWindow, on the main thread that owns it.
        unsafe { msg_send![&*ns_window, setAlphaValue: 0.0f64] }
        let _ = window.set_ignore_cursor_events(true);
        window.set_visible(true);
    }
    #[cfg(not(target_os = "macos"))]
    let _ = window;
}
