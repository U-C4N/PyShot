//! Synchronous Media Foundation encoder: the caller owns pacing and A/V timestamps.
use anyhow::{Context, Result, ensure};
use std::{path::Path, ptr};
use windows::{Win32::Media::MediaFoundation::*, core::PCWSTR};

pub struct Encoder {
    writer: IMFSinkWriter,
    video: u32,
    audio: Option<u32>,
    width: u32,
    height: u32,
    fps: u32,
    audio_frames: u64,
    finished: bool,
}

pub fn ticks(index: u64, rate: u32) -> i64 {
    (index as u128 * 10_000_000 / rate as u128) as i64
}

impl Encoder {
    pub fn new(path: &Path, width: u32, height: u32, fps: u32, audio: bool) -> Result<Self> {
        ensure!(
            width >= 2 && height >= 2 && width % 2 == 0 && height % 2 == 0,
            "H.264 dimensions must be even"
        );
        ensure!((1..=60).contains(&fps), "Invalid frame rate");
        // Software transforms are available on supported Windows editions with Media Foundation.
        // Avoid hardware-only negotiation: driver support varies across installed GPUs.
        unsafe {
            let mut attrs = None;
            MFCreateAttributes(&mut attrs, 3)?;
            let attrs = attrs.unwrap();
            attrs.SetGUID(&MF_TRANSCODE_CONTAINERTYPE, &MFTranscodeContainerType_MPEG4)?;
            attrs.SetUINT32(&MF_READWRITE_ENABLE_HARDWARE_TRANSFORMS, 0)?;
            let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
            let writer = MFCreateSinkWriterFromURL(PCWSTR(wide.as_ptr()), None, &attrs)
                .context("Cannot create MP4 writer")?;
            let output = MFCreateMediaType()?;
            output.SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Video)?;
            output.SetGUID(&MF_MT_SUBTYPE, &MFVideoFormat_H264)?;
            output.SetUINT32(
                &MF_MT_AVG_BITRATE,
                (width as u64 * height as u64 * fps as u64 / 8).clamp(1_000_000, 30_000_000) as u32,
            )?;
            output.SetUINT32(&MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive.0 as u32)?;
            video_geometry(&output, width, height, fps)?;
            let video = writer.AddStream(&output)?;
            let input = MFCreateMediaType()?;
            input.SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Video)?;
            input.SetGUID(&MF_MT_SUBTYPE, &MFVideoFormat_RGB32)?;
            input.SetUINT32(&MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive.0 as u32)?;
            input.SetUINT32(&MF_MT_DEFAULT_STRIDE, width * 4)?;
            video_geometry(&input, width, height, fps)?;
            writer.SetInputMediaType(video, &input, None).context(
                "H.264 encoder is unavailable (install Windows Media Feature Pack on N editions)",
            )?;
            let audio_index = if audio {
                let output = MFCreateMediaType()?;
                output.SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Audio)?;
                output.SetGUID(&MF_MT_SUBTYPE, &MFAudioFormat_AAC)?;
                output.SetUINT32(&MF_MT_AUDIO_NUM_CHANNELS, 2)?;
                output.SetUINT32(&MF_MT_AUDIO_SAMPLES_PER_SECOND, 48_000)?;
                output.SetUINT32(&MF_MT_AUDIO_BITS_PER_SAMPLE, 16)?;
                output.SetUINT32(&MF_MT_AUDIO_AVG_BYTES_PER_SECOND, 24_000)?;
                let index = writer.AddStream(&output)?;
                let input = MFCreateMediaType()?;
                input.SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Audio)?;
                input.SetGUID(&MF_MT_SUBTYPE, &MFAudioFormat_PCM)?;
                input.SetUINT32(&MF_MT_AUDIO_NUM_CHANNELS, 2)?;
                input.SetUINT32(&MF_MT_AUDIO_SAMPLES_PER_SECOND, 48_000)?;
                input.SetUINT32(&MF_MT_AUDIO_BITS_PER_SAMPLE, 16)?;
                input.SetUINT32(&MF_MT_AUDIO_BLOCK_ALIGNMENT, 4)?;
                input.SetUINT32(&MF_MT_AUDIO_AVG_BYTES_PER_SECOND, 192_000)?;
                writer
                    .SetInputMediaType(index, &input, None)
                    .context("AAC encoder is unavailable")?;
                Some(index)
            } else {
                None
            };
            writer.BeginWriting()?;
            Ok(Self {
                writer,
                video,
                audio: audio_index,
                width,
                height,
                fps,
                audio_frames: 0,
                finished: false,
            })
        }
    }

    pub fn video(&mut self, bgra: &[u8], index: u64) -> Result<()> {
        ensure!(
            bgra.len() == self.width as usize * self.height as usize * 4,
            "Invalid BGRA frame size"
        );
        unsafe {
            let sample = make_sample(
                bgra,
                ticks(index, self.fps),
                ticks(index + 1, self.fps) - ticks(index, self.fps),
            )?;
            self.writer
                .WriteSample(self.video, &sample)
                .context("Writing H.264 frame")?;
        }
        Ok(())
    }

    pub fn audio(&mut self, pcm: &[i16]) -> Result<()> {
        ensure!(pcm.len() % 2 == 0, "Invalid stereo audio buffer");
        let Some(stream) = self.audio else {
            return Ok(());
        };
        if pcm.is_empty() {
            return Ok(());
        }
        let count = (pcm.len() / 2) as u64;
        unsafe {
            let bytes = std::slice::from_raw_parts(pcm.as_ptr().cast::<u8>(), pcm.len() * 2);
            let sample = make_sample(
                bytes,
                ticks(self.audio_frames, 48_000),
                ticks(self.audio_frames + count, 48_000) - ticks(self.audio_frames, 48_000),
            )?;
            self.writer
                .WriteSample(stream, &sample)
                .context("Writing AAC audio")?;
        }
        self.audio_frames += count;
        Ok(())
    }

    pub fn finish(&mut self) -> Result<()> {
        if !self.finished {
            self.finished = true;
            unsafe {
                self.writer.Finalize().context("Finalizing MP4")?;
            }
        }
        Ok(())
    }
}

impl Drop for Encoder {
    fn drop(&mut self) {
        let _ = self.finish();
    }
}

use std::os::windows::ffi::OsStrExt;

unsafe fn video_geometry(mt: &IMFMediaType, w: u32, h: u32, fps: u32) -> windows::core::Result<()> {
    unsafe {
        mt.SetUINT64(&MF_MT_FRAME_SIZE, ((w as u64) << 32) | h as u64)?;
        mt.SetUINT64(&MF_MT_FRAME_RATE, ((fps as u64) << 32) | 1)?;
        mt.SetUINT64(&MF_MT_PIXEL_ASPECT_RATIO, (1 << 32) | 1)
    }
}

unsafe fn make_sample(bytes: &[u8], time: i64, duration: i64) -> windows::core::Result<IMFSample> {
    unsafe {
        let buffer = MFCreateMemoryBuffer(bytes.len() as u32)?;
        let mut target = ptr::null_mut();
        buffer.Lock(&mut target, None, None)?;
        ptr::copy_nonoverlapping(bytes.as_ptr(), target, bytes.len());
        buffer.Unlock()?;
        buffer.SetCurrentLength(bytes.len() as u32)?;
        let sample = MFCreateSample()?;
        sample.AddBuffer(&buffer)?;
        sample.SetSampleTime(time)?;
        sample.SetSampleDuration(duration)?;
        Ok(sample)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rational_timestamps_do_not_drift() {
        assert_eq!(ticks(30 * 3600, 30), 36_000_000_000);
        assert_eq!(ticks(48_000 * 3600, 48_000), 36_000_000_000);
        assert_eq!(
            (0..30)
                .map(|i| ticks(i + 1, 30) - ticks(i, 30))
                .sum::<i64>(),
            10_000_000
        );
    }
}
