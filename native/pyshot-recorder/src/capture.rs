//! Windows Graphics Capture producer with a single replaceable frame slot.
use anyhow::{Result, anyhow, ensure};
use serde::Deserialize;
use std::{
    ffi::c_void,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};
use windows::Win32::{
    Foundation::{HANDLE, HWND},
    Graphics::Gdi::{
        GetMonitorInfoW, HMONITOR, MONITORINFO, RDW_ALLCHILDREN, RDW_FRAME, RDW_INVALIDATE,
        RedrawWindow,
    },
    System::StationsAndDesktops::{
        CloseDesktop, DESKTOP_READOBJECTS, GetUserObjectInformationW, OpenInputDesktop, UOI_NAME,
    },
    UI::WindowsAndMessaging::IsWindow,
};
use windows_capture::{
    capture::{CaptureControl, Context, GraphicsCaptureApiHandler},
    frame::Frame,
    graphics_capture_api::{GraphicsCaptureApi, InternalCaptureControl},
    monitor::Monitor,
    settings::*,
    window::Window,
};

#[derive(Debug, Deserialize)]
pub struct Target {
    pub kind: String,
    #[serde(default)]
    pub hmonitor: usize,
    #[serde(default)]
    pub hwnd: usize,
    #[serde(default)]
    pub x: i32,
    #[serde(default)]
    pub y: i32,
    #[serde(default)]
    pub width: u32,
    #[serde(default)]
    pub height: u32,
}

pub struct OwnedFrame {
    pub width: u32,
    pub height: u32,
    pub qpc_100ns: i64,
    pub bgra: Vec<u8>,
}

type Mailbox = Arc<Mutex<Option<OwnedFrame>>>;

pub struct Flags {
    mailbox: Mailbox,
    closed: Arc<AtomicBool>,
    crop: Option<(u32, u32, u32, u32)>,
}

pub struct Handler {
    flags: Flags,
}

impl GraphicsCaptureApiHandler for Handler {
    type Flags = Flags;
    type Error = String;
    fn new(ctx: Context<Self::Flags>) -> std::result::Result<Self, String> {
        Ok(Self { flags: ctx.flags })
    }
    fn on_frame_arrived(
        &mut self,
        frame: &mut Frame,
        control: InternalCaptureControl,
    ) -> std::result::Result<(), String> {
        let (output_width, output_height) = self
            .flags
            .crop
            .map(|(_, _, w, h)| (w, h))
            .unwrap_or((frame.width(), frame.height()));
        if output_width > 7680 || output_height > 4320 {
            return Err("Capture frame exceeds the supported 7680x4320 dimensions".into());
        }
        let qpc_100ns = frame.timestamp().map_err(|e| e.to_string())?.Duration;
        let mut buffer = if let Some((x, y, w, h)) = self.flags.crop {
            if x + w > frame.width() || y + h > frame.height() {
                self.flags.closed.store(true, Ordering::Release);
                control.stop();
                return Ok(());
            }
            frame.buffer_crop(x, y, x + w, y + h)
        } else {
            frame.buffer()
        }
        .map_err(|e| e.to_string())?;
        let (width, height, stride) =
            (buffer.width(), buffer.height(), buffer.row_pitch() as usize);
        if width == 0 || height == 0 {
            return Ok(());
        }
        let row = width as usize * 4;
        let src = buffer.as_raw_buffer();
        let mut bgra = vec![0; row * height as usize];
        for y in 0..height as usize {
            bgra[y * row..(y + 1) * row].copy_from_slice(&src[y * stride..y * stride + row]);
        }
        *self
            .flags
            .mailbox
            .lock()
            .map_err(|_| "Capture mailbox poisoned")? = Some(OwnedFrame {
            width,
            height,
            qpc_100ns,
            bgra,
        });
        Ok(())
    }
    fn on_closed(&mut self) -> std::result::Result<(), String> {
        self.flags.closed.store(true, Ordering::Release);
        Ok(())
    }
}

pub struct Session {
    control: Option<CaptureControl<Handler, String>>,
    mailbox: Mailbox,
    closed: Arc<AtomicBool>,
    hwnd: usize,
}

impl Session {
    pub fn start(target: &Target) -> Result<Self> {
        ensure!(!desktop_locked(), "Desktop is locked or unavailable");
        let mailbox = Arc::new(Mutex::new(None));
        let closed = Arc::new(AtomicBool::new(false));
        let mut crop = None;
        let control = if target.kind == "window" {
            ensure!(
                target.hwnd != 0
                    && unsafe { IsWindow(Some(HWND(target.hwnd as *mut c_void))).as_bool() },
                "Window handle is invalid"
            );
            Handler::start_free_threaded(settings(
                Window::from_raw_hwnd(target.hwnd as *mut c_void),
                Flags {
                    mailbox: mailbox.clone(),
                    closed: closed.clone(),
                    crop,
                },
            ))
        } else {
            ensure!(
                target.kind == "monitor" || target.kind == "region",
                "Target kind must be monitor, window or region"
            );
            ensure!(target.hmonitor != 0, "Monitor handle is required");
            let mut info = MONITORINFO {
                cbSize: std::mem::size_of::<MONITORINFO>() as u32,
                ..Default::default()
            };
            ensure!(
                unsafe {
                    GetMonitorInfoW(HMONITOR(target.hmonitor as *mut c_void), &mut info).as_bool()
                },
                "Monitor handle is invalid"
            );
            if target.kind == "region" {
                let x = target.x as i64 - info.rcMonitor.left as i64;
                let y = target.y as i64 - info.rcMonitor.top as i64;
                let mw = (info.rcMonitor.right - info.rcMonitor.left) as i64;
                let mh = (info.rcMonitor.bottom - info.rcMonitor.top) as i64;
                ensure!(
                    target.width > 0
                        && target.height > 0
                        && x >= 0
                        && y >= 0
                        && x + target.width as i64 <= mw
                        && y + target.height as i64 <= mh,
                    "Region must fit inside the selected monitor"
                );
                crop = Some((x as u32, y as u32, target.width, target.height));
            }
            Handler::start_free_threaded(settings(
                Monitor::from_raw_hmonitor(target.hmonitor as *mut c_void),
                Flags {
                    mailbox: mailbox.clone(),
                    closed: closed.clone(),
                    crop,
                },
            ))
        }
        .map_err(|e| anyhow!("Cannot start Windows Graphics Capture: {e:?}"))?;
        let session = Self {
            control: Some(control),
            mailbox,
            closed,
            hwnd: target.hwnd,
        };
        if target.kind == "window" {
            // A long-idle window can need a repaint before WGC delivers its first frame.
            // Queue that normal repaint without changing the window's content. Do not use
            // UPDATENOW: a hung target must never block our independent stop path.
            unsafe {
                let _ = RedrawWindow(
                    Some(HWND(target.hwnd as *mut c_void)),
                    None,
                    None,
                    RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_FRAME,
                );
            }
        }
        Ok(session)
    }

    pub fn take(&self) -> Option<OwnedFrame> {
        self.mailbox.lock().ok()?.take()
    }
    pub fn interrupted(&self) -> Option<&'static str> {
        if desktop_locked() {
            return Some("desktop_locked");
        }
        if self.closed.load(Ordering::Acquire)
            || (self.hwnd != 0
                && !unsafe { IsWindow(Some(HWND(self.hwnd as *mut c_void))).as_bool() })
        {
            return Some("target_closed");
        }
        if self.control.as_ref().is_some_and(|c| c.is_finished()) {
            return Some("capture_stopped");
        }
        None
    }
    pub fn stop(&mut self) -> Result<()> {
        if let Some(control) = self.control.take() {
            control
                .stop()
                .map_err(|e| anyhow!("Stopping capture: {e:?}"))?;
        }
        Ok(())
    }
}
impl Drop for Session {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

fn settings<T: TryInto<GraphicsCaptureItemType>>(target: T, flags: Flags) -> Settings<Flags, T> {
    // Throttle the producer when supported; older Windows 10 builds retain their default.
    // Our independent output clock still repeats static frames at 30fps.
    let interval = if GraphicsCaptureApi::is_minimum_update_interval_supported().unwrap_or(false) {
        MinimumUpdateIntervalSettings::Custom(std::time::Duration::from_nanos(33_333_333))
    } else {
        MinimumUpdateIntervalSettings::Default
    };
    Settings::new(
        target,
        CursorCaptureSettings::Default,
        DrawBorderSettings::Default,
        SecondaryWindowSettings::Default,
        interval,
        DirtyRegionSettings::Default,
        ColorFormat::Bgra8,
        flags,
    )
}

pub fn desktop_locked() -> bool {
    unsafe {
        let Ok(desktop) = OpenInputDesktop(Default::default(), false, DESKTOP_READOBJECTS) else {
            return true;
        };
        let mut name = [0u16; 256];
        let mut needed = 0;
        let ok = GetUserObjectInformationW(
            HANDLE(desktop.0),
            UOI_NAME,
            Some(name.as_mut_ptr().cast()),
            std::mem::size_of_val(&name) as u32,
            Some(&mut needed),
        );
        let _ = CloseDesktop(desktop);
        if ok.is_err() {
            return true;
        }
        let len = name.iter().position(|v| *v == 0).unwrap_or(name.len());
        !String::from_utf16_lossy(&name[..len]).eq_ignore_ascii_case("Default")
    }
}

/// Preserve a fixed output canvas when a captured window changes size.
pub fn letterbox(frame: &OwnedFrame, width: u32, height: u32) -> Vec<u8> {
    if frame.width == width && frame.height == height {
        return frame.bgra.clone();
    }
    let mut out = vec![0; width as usize * height as usize * 4];
    for a in out.iter_mut().skip(3).step_by(4) {
        *a = 255;
    }
    // Do not enlarge a smaller window. Odd dimensions gain black padding, not stretched pixels.
    let scale = (width as f64 / frame.width as f64)
        .min(height as f64 / frame.height as f64)
        .min(1.0);
    let w = (frame.width as f64 * scale).floor().max(1.0) as u32;
    let h = (frame.height as f64 * scale).floor().max(1.0) as u32;
    let ox = (width - w) / 2;
    let oy = (height - h) / 2;
    for y in 0..h {
        let sy = y as u64 * frame.height as u64 / h as u64;
        for x in 0..w {
            let sx = x as u64 * frame.width as u64 / w as u64;
            let src = ((sy * frame.width as u64 + sx) * 4) as usize;
            let dst = (((y + oy) * width + x + ox) * 4) as usize;
            out[dst..dst + 4].copy_from_slice(&frame.bgra[src..src + 4]);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn odd_frame_padding_preserves_edges_and_orientation() {
        let f = OwnedFrame {
            width: 3,
            height: 1,
            qpc_100ns: 0,
            bgra: vec![0, 0, 255, 255, 0, 255, 0, 255, 255, 0, 0, 255],
        };
        let b = letterbox(&f, 4, 2);
        assert_eq!(&b[..12], &f.bgra);
        assert_eq!(&b[12..16], &[0, 0, 0, 255]);
        assert_eq!(&b[16..20], &[0, 0, 0, 255]);
    }
    #[test]
    fn resized_window_is_letterboxed() {
        let f = OwnedFrame {
            width: 4,
            height: 2,
            qpc_100ns: 0,
            bgra: [20, 30, 40, 255].repeat(8),
        };
        let b = letterbox(&f, 2, 4);
        assert_eq!(&b[0..4], &[0, 0, 0, 255]);
        assert_eq!(&b[8..12], &[20, 30, 40, 255]);
        assert_eq!(&b[24..28], &[0, 0, 0, 255]);
    }
}
