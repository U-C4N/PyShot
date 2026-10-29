mod audio;
mod capture;
mod encoder;
mod frames;

use anyhow::{Context, Result, anyhow, bail, ensure};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    io::{self, BufRead, Write},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};
use windows::Win32::{
    Media::MediaFoundation::{MF_VERSION, MFSTARTUP_FULL, MFShutdown, MFStartup},
    System::{
        Performance::{QueryPerformanceCounter, QueryPerformanceFrequency},
        WinRT::{RO_INIT_MULTITHREADED, RoInitialize, RoUninitialize},
    },
    UI::HiDpi::{DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, SetProcessDpiAwarenessContext},
};

#[derive(Deserialize)]
struct Request {
    target: capture::Target,
    output_path: PathBuf,
    #[serde(default = "default_fps")]
    fps: u32,
    #[serde(default = "default_audio")]
    audio: String,
    #[serde(default)]
    microphone_id: Option<String>,
    #[serde(default)]
    system_audio_id: Option<String>,
    #[serde(default)]
    duration_seconds: Option<f64>,
}
fn default_fps() -> u32 {
    30
}
fn default_audio() -> String {
    "none".into()
}

struct Runtime;
impl Runtime {
    fn new() -> Result<Self> {
        unsafe {
            let _ = SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
            RoInitialize(RO_INIT_MULTITHREADED)?;
            if let Err(e) = MFStartup(MF_VERSION, MFSTARTUP_FULL) {
                RoUninitialize();
                return Err(e.into());
            }
        }
        Ok(Self)
    }
}
impl Drop for Runtime {
    fn drop(&mut self) {
        unsafe {
            let _ = MFShutdown();
            RoUninitialize();
        }
    }
}

fn emit(value: Value) -> Result<()> {
    let mut stdout = io::stdout().lock();
    serde_json::to_writer(&mut stdout, &value)?;
    writeln!(stdout)?;
    stdout.flush()?;
    Ok(())
}

fn qpc_100ns() -> Result<i64> {
    let (mut count, mut frequency) = (0, 0);
    unsafe {
        QueryPerformanceCounter(&mut count)?;
        QueryPerformanceFrequency(&mut frequency)?;
    }
    Ok((count as i128 * 10_000_000 / frequency as i128) as i64)
}

fn record() -> Result<()> {
    let mut line = String::new();
    ensure!(
        io::stdin().read_line(&mut line)? > 0,
        "Missing recording request"
    );
    let request: Request = serde_json::from_str(&line).context("Invalid recording request")?;
    ensure!(request.fps == 30, "This version records at 30 fps");
    ensure!(
        ["none", "microphone", "system", "both"].contains(&request.audio.as_str()),
        "Invalid audio mode"
    );
    if let Some(d) = request.duration_seconds {
        ensure!(
            d.is_finite() && d > 0.0 && d <= 86400.0,
            "Duration must be between 0 and 86400 seconds"
        );
    }
    ensure!(!request.output_path.exists(), "Output path already exists");
    let stop = Arc::new(AtomicBool::new(false));
    let stop_reader = stop.clone();
    thread::spawn(move || {
        for line in io::stdin().lock().lines() {
            match line {
                Ok(line) => {
                    if serde_json::from_str::<Value>(&line)
                        .ok()
                        .is_some_and(|v| v["op"] == "stop")
                    {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
        // Parent exit or broken IPC is also a request to finalize.
        stop_reader.store(true, Ordering::Release);
    });
    let mut audio = audio::AudioCapture::start(
        &request.audio,
        &request.microphone_id,
        &request.system_audio_id,
    )
    .map_err(|e| anyhow!(e))?;
    let mut capture = capture::Session::start(&request.target)?;
    let waiting = Instant::now();
    let mut restarted = false;
    let first = loop {
        if stop.load(Ordering::Acquire) {
            bail!("Recording cancelled before the first frame")
        }
        if let Some(reason) = capture.interrupted() {
            bail!("Capture interrupted before the first frame: {reason}")
        }
        if let Some(frame) = capture.take() {
            break frame;
        }
        // A compositor session may survive briefly after a previous capture closes.
        // Recreate our session once, without changing or sending input to the target.
        if !restarted && waiting.elapsed() >= Duration::from_secs(3) {
            capture.stop()?;
            capture = capture::Session::start(&request.target)?;
            restarted = true;
        }
        ensure!(
            waiting.elapsed() < Duration::from_secs(10),
            "No video frame received within 10 seconds"
        );
        thread::sleep(Duration::from_millis(10));
    };
    let width = (first.width + 1) & !1;
    let height = (first.height + 1) & !1;
    ensure!(
        width <= 7680 && height <= 4320,
        "Recording dimensions exceed 7680x4320"
    );
    let origin = first.qpc_100ns;
    let mut bgra = capture::letterbox(&first, width, height);
    let mut encoder = encoder::Encoder::new(
        &request.output_path,
        width,
        height,
        request.fps,
        request.audio != "none",
    )?;
    encoder.video(&bgra, 0)?;
    emit(json!({"event":"started", "width":width, "height":height}))?;
    let mut count = 1u64;
    let mut next_index = 1u64;
    let mut last_end_index = 1u64;
    let mut last_progress = Instant::now();
    let outcome = (|| -> Result<Option<String>> {
        loop {
            if stop.load(Ordering::Acquire) {
                break Ok(None);
            }
            if let Some(reason) = capture.interrupted() {
                break Ok(Some(reason.to_owned()));
            }
            let elapsed = (qpc_100ns()? - origin).max(0);
            if request
                .duration_seconds
                .is_some_and(|d| elapsed as f64 >= d * 10_000_000.0)
                && count > 0
            {
                break Ok(None);
            }
            if elapsed < encoder::ticks(next_index, request.fps) {
                thread::sleep(Duration::from_millis(2));
                continue;
            }
            if let Some(frame) = capture.take() {
                bgra = capture::letterbox(&frame, width, height);
            }
            // On overload drop overdue slots instead of letting an unbounded queue accumulate.
            let index = next_index.max((elapsed as u64 * request.fps as u64) / 10_000_000);
            encoder.video(&bgra, index)?;
            count += 1;
            next_index = index + 1;
            last_end_index = next_index;
            let audio_end = (next_index * 48_000 / request.fps as u64).saturating_sub(4_800);
            encoder.audio(&audio.drain(origin, audio_end).map_err(|e| anyhow!(e))?)?;
            if last_progress.elapsed() >= Duration::from_secs(1) {
                emit(
                    json!({"event":"progress", "duration_seconds":next_index as f64 / request.fps as f64,"frames":count}),
                )?;
                last_progress = Instant::now();
            }
        }
    })();
    let mut interrupted = match outcome {
        Ok(reason) => reason,
        Err(e) => Some(format!("recording_error: {e:#}")),
    };
    if let Err(e) = capture.stop() {
        interrupted.get_or_insert_with(|| format!("capture_error: {e:#}"));
    }
    match audio.finish(origin, last_end_index * 48_000 / request.fps as u64) {
        Ok((pcm, cause)) => {
            if let Some(cause) = cause {
                interrupted.get_or_insert_with(|| format!("audio_error: {cause}"));
            }
            if let Err(e) = encoder.audio(&pcm) {
                interrupted.get_or_insert_with(|| format!("audio_error: {e:#}"));
            }
        }
        Err(e) => {
            interrupted.get_or_insert_with(|| format!("audio_error: {e}"));
        }
    }
    ensure!(count > 0, "Recording stopped before a frame was written");
    encoder.finish()?;
    emit(
        json!({"event":"completed", "duration_seconds":last_end_index as f64 / request.fps as f64, "frames":count, "width":width,"height":height,"interrupted":interrupted}),
    )
}

fn argument(args: &[String], name: &str) -> Result<String> {
    args.windows(2)
        .find(|a| a[0] == name)
        .map(|a| a[1].clone())
        .ok_or_else(|| anyhow!("Missing {name}"))
}

fn screenshot(target: &capture::Target, output: &Path) -> Result<()> {
    use image::{ExtendedColorType, ImageEncoder, codecs::png::PngEncoder};
    ensure!(!output.exists(), "Output path already exists");
    let mut capture = capture::Session::start(target)?;
    let waiting = Instant::now();
    let mut restarted = false;
    let frame = loop {
        if let Some(reason) = capture.interrupted() {
            bail!("Screenshot interrupted: {reason}")
        }
        if let Some(frame) = capture.take() {
            break frame;
        }
        if !restarted && waiting.elapsed() >= Duration::from_secs(3) {
            capture.stop()?;
            capture = capture::Session::start(target)?;
            restarted = true;
        }
        ensure!(
            waiting.elapsed() < Duration::from_secs(10),
            "No screenshot frame received within 10 seconds"
        );
        thread::sleep(Duration::from_millis(10));
    };
    capture.stop()?;
    let mut rgb = Vec::with_capacity((frame.width * frame.height * 3) as usize);
    for pixel in frame.bgra.chunks_exact(4) {
        rgb.extend_from_slice(&[pixel[2], pixel[1], pixel[0]]);
    }
    let file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(output)?;
    let mut writer = io::BufWriter::new(file);
    let result = (|| -> Result<()> {
        PngEncoder::new(&mut writer).write_image(
            &rgb,
            frame.width,
            frame.height,
            ExtendedColorType::Rgb8,
        )?;
        writer.flush()?;
        Ok(())
    })();
    drop(writer);
    if let Err(error) = result {
        let _ = std::fs::remove_file(output);
        return Err(error);
    }
    emit(json!({"path":output,"width":frame.width,"height":frame.height}))
}

/// Synthetic media verifies installed H.264/AAC encoders without accessing a screen or audio device.
fn self_test(path: &Path) -> Result<()> {
    ensure!(!path.exists(), "Output path already exists");
    let (w, h) = (320, 240);
    let mut encoder = encoder::Encoder::new(path, w, h, 30, true)?;
    let mut bgra = vec![0; (w * h * 4) as usize];
    for y in 0..h {
        for x in 0..w {
            let i = ((y * w + x) * 4) as usize;
            bgra[i..i + 4].copy_from_slice(if y < h / 2 {
                &[0, 0, 255, 255]
            } else {
                &[255, 0, 0, 255]
            });
        }
    }
    for frame in 0..60 {
        encoder.video(&bgra, frame)?;
        let mut pcm = Vec::with_capacity(3200);
        for sample in frame * 1600..(frame + 1) * 1600 {
            let value =
                ((sample as f64 * 440.0 * std::f64::consts::TAU / 48_000.0).sin() * 8_000.0) as i16;
            pcm.extend_from_slice(&[value, value]);
        }
        encoder.audio(&pcm)?;
    }
    encoder.finish()?;
    emit(
        json!({"ok":true,"path":path,"frames":60,"duration_seconds":2.0,"width":w,"height":h,"audio":"synthetic_440hz"}),
    )
}

fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let _runtime = Runtime::new().context("Windows Media Foundation is unavailable")?;
    match args.first().map(String::as_str) {
        Some("capabilities") => emit(
            json!({"version":"3.0.1","engine":"windows-graphics-capture/media-foundation","available":windows::Graphics::Capture::GraphicsCaptureSession::IsSupported().unwrap_or(false),"video_codec":"h264","audio_codec":"aac","container":"mp4","fps":30,"targets":["monitor","window","region"],"audio":["none","microphone","system","both"],"pause":false}),
        ),
        Some("audio-devices") => emit(audio::enumerate_devices().map_err(|e| anyhow!(e))?),
        Some("record") => record(),
        Some("screenshot") => {
            let target: capture::Target = serde_json::from_str(&argument(&args, "--target")?)?;
            screenshot(&target, Path::new(&argument(&args, "--output")?))
        }
        Some("frames") => {
            let path = argument(&args, "--path")?;
            let timestamps: Vec<f64> = serde_json::from_str(&argument(&args, "--timestamps")?)?;
            let output = argument(&args, "--output-dir")?;
            emit(
                frames::run(Path::new(&path), &timestamps, Path::new(&output))
                    .map_err(|e| anyhow!(e))?,
            )
        }
        Some("self-test") => self_test(Path::new(&argument(&args, "--output")?)),
        _ => bail!(
            "Usage: pyshot-recorder capabilities | audio-devices | record | screenshot --target JSON --output FILE | frames --path FILE --timestamps '[0,1]' --output-dir DIR | self-test --output FILE"
        ),
    }
}

fn main() {
    if let Err(error) = run() {
        let _ = emit(json!({"event":"error","code":"native_error","message":format!("{error:#}")}));
        eprintln!("{error:#}");
        std::process::exit(1);
    }
}
