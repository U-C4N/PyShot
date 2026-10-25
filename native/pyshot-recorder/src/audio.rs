//! WASAPI endpoint capture and QPC-clocked stereo mixing.
//!
//! Endpoint threads own all their COM interfaces. Audio is placed on the video
//! QPC timeline packet by packet; nominal device rates never accumulate drift.
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};
use windows::{
    Win32::{
        Devices::FunctionDiscovery::PKEY_Device_FriendlyName,
        Media::Audio::*,
        System::{Com::*, Performance::*},
    },
    core::{BSTR, GUID, PCWSTR},
};

const RATE: u32 = 48_000;
const HNS: f64 = 10_000_000.0;
const MAX_QUEUED_PACKETS: usize = 256;

#[derive(Clone)]
struct Packet {
    qpc: i64,
    rate: u32,
    samples: Vec<[f32; 2]>,
}
#[derive(Default)]
struct SourceState {
    packets: VecDeque<Packet>,
    error: Option<String>,
    latest_end: i64,
}
struct Source {
    state: Arc<Mutex<SourceState>>,
    thread: Option<thread::JoinHandle<()>>,
}

pub struct AudioCapture {
    sources: Vec<Source>,
    stopped: Arc<AtomicBool>,
    cursor: u64,
}

impl AudioCapture {
    pub fn start(
        mode: &str,
        microphone_id: &Option<String>,
        system_id: &Option<String>,
    ) -> Result<Self, String> {
        let selections = match mode {
            "none" => vec![],
            "microphone" => vec![(false, microphone_id.clone())],
            "system" => vec![(true, system_id.clone())],
            "both" => vec![(false, microphone_id.clone()), (true, system_id.clone())],
            _ => return Err(format!("Unknown audio mode: {mode}")),
        };
        let mut capture = Self {
            sources: vec![],
            stopped: Arc::new(AtomicBool::new(false)),
            cursor: 0,
        };
        for (loopback, id) in selections {
            let state = Arc::new(Mutex::new(SourceState::default()));
            let worker_state = state.clone();
            let stop = capture.stopped.clone();
            let (tx, rx) = mpsc::sync_channel(1);
            let worker = thread::Builder::new()
                .name(
                    if loopback {
                        "wasapi-system"
                    } else {
                        "wasapi-microphone"
                    }
                    .into(),
                )
                .spawn(move || {
                    let initialized = unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) };
                    if let Err(error) = initialized.ok() {
                        let _ = tx.send(Err(error.to_string()));
                        return;
                    }
                    let result = unsafe {
                        capture_endpoint(loopback, id.as_deref(), &stop, &worker_state, &tx)
                    };
                    if let Err(error) = result {
                        let _ = tx.try_send(Err(error.clone()));
                        worker_state.lock().unwrap_or_else(|e| e.into_inner()).error = Some(error);
                    }
                    unsafe {
                        CoUninitialize();
                    }
                })
                .map_err(|e| e.to_string())?;
            capture.sources.push(Source {
                state,
                thread: Some(worker),
            });
            match rx.recv_timeout(Duration::from_secs(10)) {
                Ok(Ok(())) => {}
                Ok(Err(error)) => {
                    capture.stop();
                    return Err(error);
                }
                Err(error) => {
                    capture.stop();
                    return Err(format!("Audio endpoint startup failed: {error}"));
                }
            }
        }
        Ok(capture)
    }

    /// Return interleaved PCM16 stereo frames [previous_end, end_frame).
    /// The caller normally keeps audio 100 ms behind video and flushes on stop.
    pub fn drain(&mut self, origin_qpc: i64, end_frame: u64) -> Result<Vec<i16>, String> {
        self.drain_inner(origin_qpc, end_frame, false)
    }

    /// Preserve already captured packets even when a device failed. The caller
    /// must retain the returned interruption reason in its completed result.
    pub fn finish(
        &mut self,
        origin_qpc: i64,
        end_frame: u64,
    ) -> Result<(Vec<i16>, Option<String>), String> {
        self.stop();
        let error = self.sources.iter().find_map(|source| {
            source
                .state
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .error
                .clone()
        });
        Ok((self.drain_inner(origin_qpc, end_frame, true)?, error))
    }

    fn drain_inner(
        &mut self,
        origin_qpc: i64,
        end_frame: u64,
        final_flush: bool,
    ) -> Result<Vec<i16>, String> {
        if end_frame < self.cursor {
            return Err("Audio cursor cannot move backwards".into());
        }
        if self.sources.is_empty() {
            self.cursor = end_frame;
            return Ok(vec![]);
        }
        let count = (end_frame - self.cursor) as usize;
        if count > RATE as usize * 10 {
            return Err("Audio encoder fell more than ten seconds behind".into());
        }
        if count == 0 {
            return Ok(vec![]);
        }
        let target_qpc = origin_qpc + ((end_frame as f64 / RATE as f64) * HNS).round() as i64;
        // Loopback produces no packets when the render endpoint is silent.
        // A timeline already 40 ms in the past needs silence, not a wait on
        // every video tick. Near-live final draining can still await a packet.
        let wait = if qpc_now()? - target_qpc >= 400_000 {
            Duration::ZERO
        } else {
            Duration::from_millis(40)
        };
        let deadline = Instant::now() + wait;
        loop {
            let mut ready = true;
            for source in &self.sources {
                let state = source.state.lock().unwrap_or_else(|e| e.into_inner());
                if let Some(error) = &state.error
                    && !final_flush
                {
                    return Err(error.clone());
                }
                ready &= state.latest_end >= target_qpc;
            }
            if ready || self.stopped.load(Ordering::Relaxed) || Instant::now() >= deadline {
                break;
            }
            thread::sleep(Duration::from_millis(2));
        }
        let mut mixed = vec![[0.0_f32; 2]; count];
        let gain = if self.sources.len() > 1 { 0.5 } else { 1.0 };
        for source in &self.sources {
            let mut state = source.state.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(error) = &state.error
                && !final_flush
            {
                return Err(error.clone());
            }
            let mut source_samples = vec![[0.0_f32; 2]; count];
            for packet in &state.packets {
                place_packet(packet, origin_qpc, self.cursor, &mut source_samples);
            }
            for (mix, sample) in mixed.iter_mut().zip(source_samples) {
                mix[0] += sample[0] * gain;
                mix[1] += sample[1] * gain;
            }
            while state
                .packets
                .front()
                .is_some_and(|p| packet_end(p) <= target_qpc)
            {
                state.packets.pop_front();
            }
        }
        self.cursor = end_frame;
        Ok(mixed
            .into_iter()
            .flat_map(|frame| {
                frame.map(|sample| (sample.clamp(-1.0, 1.0) * 32767.0).round() as i16)
            })
            .collect())
    }

    pub fn stop(&mut self) {
        self.stopped.store(true, Ordering::Relaxed);
        for source in &mut self.sources {
            if let Some(worker) = source.thread.take() {
                let _ = worker.join();
            }
        }
    }
}
impl Drop for AudioCapture {
    fn drop(&mut self) {
        self.stop();
    }
}

fn packet_end(packet: &Packet) -> i64 {
    packet.qpc + (packet.samples.len() as f64 / packet.rate as f64 * HNS).round() as i64
}

fn place_packet(packet: &Packet, origin: i64, cursor: u64, destination: &mut [[f32; 2]]) {
    if packet.samples.is_empty() {
        return;
    }
    let start = (packet.qpc - origin) as f64 * RATE as f64 / HNS;
    let end = start + packet.samples.len() as f64 * RATE as f64 / packet.rate as f64;
    let from = start.round().max(cursor as f64) as u64;
    let until = end
        .round()
        .min((cursor + destination.len() as u64) as f64)
        .max(0.0) as u64;
    for output_frame in from..until {
        let source_pos =
            ((output_frame as f64 - start) * packet.rate as f64 / RATE as f64).max(0.0);
        let index = (source_pos.floor() as usize).min(packet.samples.len() - 1);
        let next = (index + 1).min(packet.samples.len() - 1);
        let fraction = (source_pos - index as f64).clamp(0.0, 1.0) as f32;
        let output = &mut destination[(output_frame - cursor) as usize];
        for channel in 0..2 {
            output[channel] = packet.samples[index][channel] * (1.0 - fraction)
                + packet.samples[next][channel] * fraction;
        }
    }
}

struct Format {
    rate: u32,
    channels: usize,
    bits: usize,
    block: usize,
    float: bool,
    channel_mask: u32,
}
impl Format {
    unsafe fn read(format: *const WAVEFORMATEX) -> Result<Self, String> {
        let wave = unsafe { std::ptr::read_unaligned(format) };
        let bits = wave.wBitsPerSample;
        let mut float = wave.wFormatTag == 3;
        let mut channel_mask = 0;
        if wave.wFormatTag == 0xfffe && wave.cbSize >= 22 {
            let ext = unsafe { std::ptr::read_unaligned(format.cast::<WAVEFORMATEXTENSIBLE>()) };
            let subtype = ext.SubFormat;
            float = subtype == GUID::from_u128(0x00000003_0000_0010_8000_00aa00389b71);
            if !float && subtype != GUID::from_u128(0x00000001_0000_0010_8000_00aa00389b71) {
                return Err("Unsupported endpoint sample format".into());
            }
            channel_mask = ext.dwChannelMask;
        } else if wave.wFormatTag != 1 && wave.wFormatTag != 3 {
            return Err("Unsupported endpoint sample format".into());
        }
        if wave.nSamplesPerSec == 0
            || wave.nChannels == 0
            || wave.nChannels > 32
            || ![8, 16, 24, 32].contains(&bits)
            || (float && wave.wBitsPerSample != 32)
            || wave.nBlockAlign as usize
                != wave.nChannels as usize * wave.wBitsPerSample as usize / 8
        {
            return Err("Invalid endpoint mix format".into());
        }
        Ok(Self {
            rate: wave.nSamplesPerSec,
            channels: wave.nChannels as usize,
            bits: wave.wBitsPerSample as usize,
            block: wave.nBlockAlign as usize,
            float,
            channel_mask,
        })
    }
    fn convert(&self, bytes: &[u8], frames: usize) -> Vec<[f32; 2]> {
        let mut output = Vec::with_capacity(frames);
        let channel_bytes = self.bits / 8;
        for frame in bytes.chunks_exact(self.block).take(frames) {
            let mut stereo = [0.0; 2];
            for channel in 0..self.channels {
                let at = channel * channel_bytes;
                let sample = if self.float {
                    f32::from_le_bytes(frame[at..at + 4].try_into().unwrap())
                } else {
                    match self.bits {
                        8 => (frame[at] as f32 - 128.0) / 128.0,
                        16 => {
                            i16::from_le_bytes(frame[at..at + 2].try_into().unwrap()) as f32
                                / 32768.0
                        }
                        24 => {
                            ((i32::from_le_bytes([0, frame[at], frame[at + 1], frame[at + 2]]))
                                as f64
                                / 2147483648.0) as f32
                        }
                        _ => {
                            (i32::from_le_bytes(frame[at..at + 4].try_into().unwrap()) as f64
                                / 2147483648.0) as f32
                        }
                    }
                };
                let sample = if sample.is_finite() { sample } else { 0.0 };
                if self.channels == 1 {
                    stereo = [sample, sample];
                    break;
                }
                let speaker = if self.channel_mask == 0 {
                    1u32 << channel
                } else {
                    (0..32)
                        .filter(|bit| self.channel_mask & (1 << bit) != 0)
                        .nth(channel)
                        .map(|bit| 1u32 << bit)
                        .unwrap_or(0)
                };
                match speaker {
                    1 => stereo[0] += sample,
                    2 => stereo[1] += sample,
                    4 => {
                        stereo[0] += sample * 0.707;
                        stereo[1] += sample * 0.707;
                    }
                    8 => {
                        stereo[0] += sample * 0.25;
                        stereo[1] += sample * 0.25;
                    }
                    16 | 64 | 512 => stereo[0] += sample * 0.707,
                    32 | 128 | 1024 => stereo[1] += sample * 0.707,
                    _ => {
                        stereo[0] += sample * 0.5;
                        stereo[1] += sample * 0.5;
                    }
                }
            }
            output.push(stereo);
        }
        output
    }
}

fn qpc_now() -> Result<i64, String> {
    unsafe {
        let mut counter = 0;
        let mut frequency = 0;
        QueryPerformanceCounter(&mut counter).map_err(|e| e.to_string())?;
        QueryPerformanceFrequency(&mut frequency).map_err(|e| e.to_string())?;
        Ok(((counter as i128 * 10_000_000) / frequency as i128) as i64)
    }
}

#[derive(Default)]
struct PacketClock {
    anchor: Option<(u64, i64)>,
}
impl PacketClock {
    fn resolve(
        &mut self,
        position: u64,
        timestamp: u64,
        flags: u32,
        rate: u32,
        frames: u32,
        now: i64,
    ) -> Result<i64, String> {
        if flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR.0 as u32 == 0 {
            let timestamp = i64::try_from(timestamp).map_err(|_| "Invalid audio timestamp")?;
            self.anchor = Some((position, timestamp));
            return Ok(timestamp);
        }
        let (anchor_position, anchor_qpc) = match self.anchor {
            Some(anchor) => anchor,
            None => {
                let anchor = (position, now - frames as i64 * 10_000_000 / rate as i64);
                self.anchor = Some(anchor);
                anchor
            }
        };
        let elapsed = position
            .checked_sub(anchor_position)
            .ok_or("Audio device clock reset without a valid timestamp")?;
        if elapsed > rate as u64 {
            return Err(
                "Audio device provided no reliable timestamp for more than one second".into(),
            );
        }
        // Keep the anchor unchanged during a run of missing timestamps so
        // rounding at 44.1 kHz cannot accumulate from packet to packet.
        Ok(anchor_qpc + (elapsed as i128 * 10_000_000 / rate as i128) as i64)
    }
}

unsafe fn capture_endpoint(
    loopback: bool,
    selected: Option<&str>,
    stop: &AtomicBool,
    shared: &Mutex<SourceState>,
    ready: &mpsc::SyncSender<Result<(), String>>,
) -> Result<(), String> {
    unsafe {
        let enumerator: IMMDeviceEnumerator =
            CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL).map_err(|e| e.to_string())?;
        let device = if let Some(id) = selected {
            let wide: Vec<u16> = id.encode_utf16().chain(Some(0)).collect();
            enumerator.GetDevice(PCWSTR(wide.as_ptr()))
        } else {
            enumerator.GetDefaultAudioEndpoint(if loopback { eRender } else { eCapture }, eConsole)
        }
        .map_err(|e| format!("Audio device unavailable: {e}"))?;
        let client: IAudioClient = device
            .Activate(CLSCTX_ALL, None)
            .map_err(|e| e.to_string())?;
        let mix = client.GetMixFormat().map_err(|e| e.to_string())?;
        let format_result = Format::read(mix);
        let initialized = client.Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            if loopback {
                AUDCLNT_STREAMFLAGS_LOOPBACK
            } else {
                0
            },
            1_000_000,
            0,
            mix,
            None,
        );
        CoTaskMemFree(Some(mix.cast()));
        let format = format_result?;
        initialized.map_err(|e| format!("Cannot start audio capture: {e}"))?;
        let capture: IAudioCaptureClient = client.GetService().map_err(|e| e.to_string())?;
        client.Start().map_err(|e| e.to_string())?;
        let _ = ready.send(Ok(()));
        let mut clock = PacketClock::default();
        let result = (|| -> Result<(), String> {
            while !stop.load(Ordering::Relaxed) {
                let mut packet_size = capture
                    .GetNextPacketSize()
                    .map_err(|e| format!("Audio device disconnected: {e}"))?;
                while packet_size > 0 {
                    let mut data = std::ptr::null_mut();
                    let mut frames = 0;
                    let mut flags = 0;
                    let mut position = 0;
                    let mut timestamp = 0;
                    capture
                        .GetBuffer(
                            &mut data,
                            &mut frames,
                            &mut flags,
                            Some(&mut position),
                            Some(&mut timestamp),
                        )
                        .map_err(|e| format!("Audio capture failed: {e}"))?;
                    let samples =
                        if flags & AUDCLNT_BUFFERFLAGS_SILENT.0 as u32 != 0 || data.is_null() {
                            vec![[0.0; 2]; frames as usize]
                        } else {
                            format.convert(
                                std::slice::from_raw_parts(data, frames as usize * format.block),
                                frames as usize,
                            )
                        };
                    capture.ReleaseBuffer(frames).map_err(|e| e.to_string())?;
                    let now = if flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR.0 as u32 != 0
                        && clock.anchor.is_none()
                    {
                        qpc_now()?
                    } else {
                        0
                    };
                    let qpc =
                        clock.resolve(position, timestamp, flags, format.rate, frames, now)?;
                    let packet = Packet {
                        qpc,
                        rate: format.rate,
                        samples,
                    };
                    let mut state = shared.lock().unwrap_or_else(|e| e.into_inner());
                    state.latest_end = state.latest_end.max(packet_end(&packet));
                    // Bound memory even while the encoder is delayed or stopping.
                    while state.packets.len() >= MAX_QUEUED_PACKETS
                        || state
                            .packets
                            .front()
                            .is_some_and(|p| qpc - p.qpc > 20_000_000)
                    {
                        state.packets.pop_front();
                    }
                    state.packets.push_back(packet);
                    drop(state);
                    packet_size = capture
                        .GetNextPacketSize()
                        .map_err(|e| format!("Audio device disconnected: {e}"))?;
                }
                thread::sleep(Duration::from_millis(3));
            }
            Ok(())
        })();
        let _ = client.Stop();
        result
    }
}

unsafe fn device_id(device: &IMMDevice) -> Result<String, String> {
    unsafe {
        let pointer = device.GetId().map_err(|e| e.to_string())?;
        let result = pointer.to_string().map_err(|e| e.to_string());
        CoTaskMemFree(Some(pointer.as_ptr().cast()));
        result
    }
}

pub fn enumerate_devices() -> Result<Value, String> {
    unsafe {
        let enumerator: IMMDeviceEnumerator =
            CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL).map_err(|e| e.to_string())?;
        let mut microphones = vec![];
        let mut system = vec![];
        for (flow, output) in [(eCapture, &mut microphones), (eRender, &mut system)] {
            let default = enumerator
                .GetDefaultAudioEndpoint(flow, eConsole)
                .ok()
                .and_then(|d| device_id(&d).ok());
            let devices = enumerator
                .EnumAudioEndpoints(flow, DEVICE_STATE_ACTIVE)
                .map_err(|e| e.to_string())?;
            for index in 0..devices.GetCount().map_err(|e| e.to_string())? {
                let device = devices.Item(index).map_err(|e| e.to_string())?;
                let id = device_id(&device)?;
                let name = device
                    .OpenPropertyStore(STGM_READ)
                    .ok()
                    .and_then(|store| store.GetValue(&PKEY_Device_FriendlyName).ok())
                    .and_then(|value| BSTR::try_from(&value).ok().map(|name| name.to_string()))
                    .unwrap_or_else(|| id.clone());
                output.push(json!({"id":id,"name":name,"is_default":default.as_ref()==Some(&id)}));
            }
        }
        Ok(
            json!({"microphones":microphones,"system_audio":system,"sample_rate":RATE,"channels":2,"modes":["none","microphone","system","both"]}),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn missing_timestamps_use_one_anchor_and_resynchronize() {
        let mut clock = PacketClock::default();
        let missing = AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR.0 as u32;
        assert_eq!(
            clock.resolve(0, 50_000_000, 0, 44_100, 441, 0).unwrap(),
            50_000_000
        );
        for position in 1..441 {
            assert_eq!(
                clock.resolve(position, 999, missing, 44_100, 1, 0).unwrap(),
                50_000_000 + position as i64 * 10_000_000 / 44_100
            );
        }
        assert_eq!(
            clock.resolve(441, 50_100_500, 0, 44_100, 441, 0).unwrap(),
            50_100_500
        );
        assert_eq!(
            clock.resolve(882, 0, missing, 44_100, 441, 0).unwrap(),
            50_200_500
        );
    }
    #[test]
    fn unavailable_audio_clock_is_bounded_and_reset_is_rejected() {
        let mut clock = PacketClock::default();
        let missing = AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR.0 as u32;
        assert_eq!(
            clock
                .resolve(100, 0, missing, 48_000, 480, 1_000_000)
                .unwrap(),
            900_000
        );
        assert!(
            clock
                .resolve(99, 0, missing, 48_000, 480, 1_000_000)
                .is_err()
        );
        assert!(
            clock
                .resolve(48_101, 0, missing, 48_000, 480, 1_000_000)
                .is_err()
        );
    }
    #[test]
    fn disconnected_endpoint_flushes_queued_audio_and_reports_reason() {
        let state = SourceState {
            packets: VecDeque::from([Packet {
                qpc: 0,
                rate: 48_000,
                samples: vec![[0.5; 2]; 480],
            }]),
            error: Some("Audio device disconnected".into()),
            latest_end: 100_000,
        };
        let mut capture = AudioCapture {
            sources: vec![Source {
                state: Arc::new(Mutex::new(state)),
                thread: None,
            }],
            stopped: Arc::new(AtomicBool::new(false)),
            cursor: 0,
        };
        assert!(capture.drain(0, 480).is_err());
        let (samples, reason) = capture.finish(0, 480).unwrap();
        assert_eq!(reason.as_deref(), Some("Audio device disconnected"));
        assert_eq!(samples.len(), 960);
        assert!(samples.iter().all(|sample| *sample == 16384));
    }
    #[test]
    fn silent_mode_does_not_accumulate_a_pending_audio_cursor() {
        let mut audio = AudioCapture::start("none", &None, &None).unwrap();
        assert!(audio.drain(0, 64 * 48_000).unwrap().is_empty());
        assert_eq!(audio.cursor, 64 * 48_000);
    }
    #[test]
    fn packets_align_to_qpc_and_trim_before_video() {
        let packet = Packet {
            qpc: 0,
            rate: 48_000,
            samples: vec![[0.75, -0.25]; 960],
        };
        let mut output = vec![[0.0; 2]; 960];
        place_packet(&packet, 100_000, 0, &mut output);
        assert_eq!(output[0], [0.75, -0.25]);
        assert_eq!(output[479], [0.75, -0.25]);
        assert_eq!(output[480], [0.0; 2]);
    }
    #[test]
    fn resampling_has_no_thirty_minute_clock_drift() {
        let origin = 5_000_000_000;
        let end = 30 * 60 * 48_000_u64;
        let packet = Packet {
            qpc: origin + 18_000_000_000 - 100_000,
            rate: 44_100,
            samples: vec![[0.5; 2]; 441],
        };
        let mut output = vec![[0.0; 2]; 480];
        place_packet(&packet, origin, end - 480, &mut output);
        assert!(output.iter().all(|sample| (sample[0] - 0.5).abs() < 0.001));
    }
    #[test]
    fn mono_pcm_and_silent_gaps() {
        let format = Format {
            rate: 48_000,
            channels: 1,
            bits: 16,
            block: 2,
            float: false,
            channel_mask: 0,
        };
        assert_eq!(format.convert(&[0, 64], 1), vec![[0.5; 2]]);
        let mut dest = vec![[0.0; 2]; 1440];
        place_packet(
            &Packet {
                qpc: 100_000,
                rate: 48_000,
                samples: vec![[1.0; 2]; 480],
            },
            0,
            0,
            &mut dest,
        );
        assert_eq!(dest[479], [0.0; 2]);
        assert_eq!(dest[480], [1.0; 2]);
        assert_eq!(dest[960], [0.0; 2]);
    }
}
