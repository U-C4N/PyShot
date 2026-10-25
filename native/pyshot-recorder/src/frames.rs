//! Decode selected MP4 frames through the Windows Media Foundation source reader.
use image::{ExtendedColorType, ImageEncoder, codecs::png::PngEncoder};
use serde_json::{Value, json};
use std::{
    fs::{self, OpenOptions},
    io::{BufWriter, Write},
    path::Path,
    time::{SystemTime, UNIX_EPOCH},
};
use windows::{
    Win32::{Media::MediaFoundation::*, System::Com::StructuredStorage::PROPVARIANT},
    core::{GUID, Interface, PCWSTR},
};

const VIDEO: u32 = MF_SOURCE_READER_FIRST_VIDEO_STREAM.0 as u32;
const ALL: u32 = MF_SOURCE_READER_ALL_STREAMS.0 as u32;

pub fn run(path: &Path, timestamps: &[f64], output_dir: &Path) -> Result<Value, String> {
    if timestamps.is_empty() || timestamps.len() > 8 {
        return Err("Request between one and eight timestamps".into());
    }
    if timestamps
        .iter()
        .any(|t| !t.is_finite() || *t < 0.0 || *t > 604_800.0)
    {
        return Err("Timestamps must be finite, nonnegative seconds".into());
    }
    if !path.is_file() {
        return Err("Completed video file does not exist".into());
    }
    fs::create_dir_all(output_dir).map_err(|e| e.to_string())?;
    unsafe {
        let mut attributes = None;
        MFCreateAttributes(&mut attributes, 2).map_err(|e| e.to_string())?;
        let attributes = attributes.ok_or("MFCreateAttributes returned no attributes")?;
        attributes
            .SetUINT32(&MF_SOURCE_READER_ENABLE_VIDEO_PROCESSING, 1)
            .map_err(|e| e.to_string())?;
        let path_wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
        let reader = MFCreateSourceReaderFromURL(PCWSTR(path_wide.as_ptr()), &attributes)
            .map_err(|e| format!("Cannot open video: {e}"))?;
        reader
            .SetStreamSelection(ALL, false)
            .map_err(|e| e.to_string())?;
        reader
            .SetStreamSelection(VIDEO, true)
            .map_err(|e| e.to_string())?;
        let native_type = reader.GetNativeMediaType(VIDEO, 0).ok();
        let output_type = MFCreateMediaType().map_err(|e| e.to_string())?;
        output_type
            .SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Video)
            .map_err(|e| e.to_string())?;
        output_type
            .SetGUID(&MF_MT_SUBTYPE, &MFVideoFormat_RGB32)
            .map_err(|e| e.to_string())?;
        reader
            .SetCurrentMediaType(VIDEO, None, &output_type)
            .map_err(|e| format!("Cannot decode RGB video: {e}"))?;
        let duration = reader
            .GetPresentationAttribute(MF_SOURCE_READER_MEDIASOURCE.0 as u32, &MF_PD_DURATION)
            .ok()
            .and_then(|value| u64::try_from(&value).ok())
            .map(|v| v as f64 / 10_000_000.0);
        if let Some(duration) = duration {
            if timestamps.iter().any(|t| *t >= duration && *t != 0.0) {
                return Err(format!(
                    "Requested timestamp is outside the video duration ({duration:.3} seconds)"
                ));
            }
        }
        let mut frames = vec![];
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|e| e.to_string())?
            .as_nanos();
        for (index, &requested) in timestamps.iter().enumerate() {
            let requested_hns = (requested * 10_000_000.0).round() as i64;
            reader
                .SetCurrentPosition(&GUID::zeroed(), &PROPVARIANT::from(requested_hns))
                .map_err(|e| format!("Cannot seek video: {e}"))?;
            let (sample, actual_timestamp) = sample_at(&reader, requested_hns)?;
            let media_type = reader
                .GetCurrentMediaType(VIDEO)
                .map_err(|e| e.to_string())?;
            let packed_size = media_type
                .GetUINT64(&MF_MT_FRAME_SIZE)
                .map_err(|e| e.to_string())?;
            let width = (packed_size >> 32) as u32;
            let height = packed_size as u32;
            if width == 0
                || height == 0
                || width > 16_384
                || height > 16_384
                || width as u64 * height as u64 > 67_108_864
            {
                return Err("Video frame dimensions are unsupported".into());
            }
            let fallback_stride = media_type
                .GetUINT32(&MF_MT_DEFAULT_STRIDE)
                .map(|v| v as i32)
                .unwrap_or((width * 4) as i32);
            let rgb = copy_rgb(&sample, width, height, fallback_stride)?;
            // H.264 decoding can expose a macroblock-aligned allocation (e.g.
            // 336x304 for a 326x296 recording). Only the display aperture is
            // content; padding must not leak into MCP previews.
            let area = display_area(&media_type, native_type.as_ref(), width, height);
            let rgb = crop_rgb(rgb, width, height, area);
            let (width, height) = (area.2, area.3);
            let destination = output_dir.join(format!("frame_{nonce}_{index:02}.png"));
            let file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&destination)
                .map_err(|e| e.to_string())?;
            let mut writer = BufWriter::new(file);
            let written = PngEncoder::new(&mut writer)
                .write_image(&rgb, width, height, ExtendedColorType::Rgb8)
                .map_err(|e| e.to_string())
                .and_then(|()| writer.flush().map_err(|e| e.to_string()));
            if let Err(error) = written {
                drop(writer);
                let _ = fs::remove_file(&destination);
                return Err(format!("Cannot write extracted frame: {error}"));
            }
            frames.push(json!({"path":destination,"timestamp_seconds":actual_timestamp as f64 / 10_000_000.0,"requested_timestamp_seconds":requested,"width":width,"height":height}));
        }
        Ok(json!({"frames":frames,"duration_seconds":duration,"video_path":path}))
    }
}

use std::os::windows::ffi::OsStrExt;

unsafe fn display_area(
    current: &IMFMediaType,
    native: Option<&IMFMediaType>,
    width: u32,
    height: u32,
) -> (u32, u32, u32, u32) {
    unsafe {
        for media_type in [Some(current), native].into_iter().flatten() {
            for key in [MF_MT_MINIMUM_DISPLAY_APERTURE, MF_MT_GEOMETRIC_APERTURE] {
                let mut bytes = [0_u8; std::mem::size_of::<MFVideoArea>()];
                if media_type.GetBlob(&key, &mut bytes, None).is_ok() {
                    let area = std::ptr::read_unaligned(bytes.as_ptr().cast::<MFVideoArea>());
                    let (x, y, w, h) = (
                        area.OffsetX.value as i32,
                        area.OffsetY.value as i32,
                        area.Area.cx,
                        area.Area.cy,
                    );
                    if x >= 0
                        && y >= 0
                        && w > 0
                        && h > 0
                        && x as u64 + w as u64 <= width as u64
                        && y as u64 + h as u64 <= height as u64
                    {
                        return (x as u32, y as u32, w as u32, h as u32);
                    }
                }
            }
        }
        if let Some(size) = native.and_then(|t| t.GetUINT64(&MF_MT_FRAME_SIZE).ok()) {
            let (w, h) = ((size >> 32) as u32, size as u32);
            if w > 0 && h > 0 && w <= width && h <= height {
                return (0, 0, w, h);
            }
        }
        (0, 0, width, height)
    }
}

fn crop_rgb(bytes: Vec<u8>, width: u32, height: u32, area: (u32, u32, u32, u32)) -> Vec<u8> {
    let (x, y, w, h) = area;
    if area == (0, 0, width, height) {
        return bytes;
    }
    let mut output = Vec::with_capacity(w as usize * h as usize * 3);
    for row in y..y + h {
        let begin = (row as usize * width as usize + x as usize) * 3;
        output.extend_from_slice(&bytes[begin..begin + w as usize * 3]);
    }
    output
}

unsafe fn sample_at(
    reader: &IMFSourceReader,
    requested_hns: i64,
) -> Result<(IMFSample, i64), String> {
    unsafe {
        let mut last = None;
        // Seeking starts at a preceding keyframe. Bound malformed-file work.
        for _ in 0..18_000 {
            let mut flags = 0;
            let mut timestamp = 0;
            let mut sample = None;
            reader
                .ReadSample(
                    VIDEO,
                    0,
                    None,
                    Some(&mut flags),
                    Some(&mut timestamp),
                    Some(&mut sample),
                )
                .map_err(|e| format!("Video decoding failed: {e}"))?;
            if flags & MF_SOURCE_READERF_ERROR.0 as u32 != 0 {
                return Err("Video decoder reported a stream error".into());
            }
            if let Some(sample) = sample {
                if timestamp >= requested_hns {
                    return Ok((sample, timestamp));
                }
                last = Some((sample, timestamp));
            }
            if flags & MF_SOURCE_READERF_ENDOFSTREAM.0 as u32 != 0 {
                return last.ok_or_else(|| "No video frame exists at the requested time".into());
            }
        }
        Err("Video seek exceeded the decoder work limit".into())
    }
}

unsafe fn copy_rgb(
    sample: &IMFSample,
    width: u32,
    height: u32,
    fallback_stride: i32,
) -> Result<Vec<u8>, String> {
    unsafe {
        let buffer = sample
            .ConvertToContiguousBuffer()
            .map_err(|e| e.to_string())?;
        if let Ok(two_d) = buffer.cast::<IMF2DBuffer>() {
            let mut scanline = std::ptr::null_mut();
            let mut stride = 0;
            two_d
                .Lock2D(&mut scanline, &mut stride)
                .map_err(|e| e.to_string())?;
            let result = rows_to_rgb(scanline, stride, width, height);
            let unlocked = two_d.Unlock2D().map_err(|e| e.to_string());
            unlocked?;
            return result;
        }
        let mut pointer = std::ptr::null_mut();
        let mut length = 0;
        buffer
            .Lock(&mut pointer, None, Some(&mut length))
            .map_err(|e| e.to_string())?;
        let stride = fallback_stride;
        let required = stride.unsigned_abs() as usize * height as usize;
        let result = if required > length as usize {
            Err("Decoded frame buffer is too small".into())
        } else {
            let scanline = if stride < 0 {
                pointer.add(stride.unsigned_abs() as usize * (height - 1) as usize)
            } else {
                pointer
            };
            rows_to_rgb(scanline, stride, width, height)
        };
        buffer.Unlock().map_err(|e| e.to_string())?;
        result
    }
}

unsafe fn rows_to_rgb(
    scanline: *const u8,
    stride: i32,
    width: u32,
    height: u32,
) -> Result<Vec<u8>, String> {
    if scanline.is_null() || stride.unsigned_abs() < width * 4 {
        return Err("Invalid decoded frame stride".into());
    }
    let mut rgb = vec![0; width as usize * height as usize * 3];
    for y in 0..height as usize {
        let row = unsafe {
            std::slice::from_raw_parts(
                scanline.offset(y as isize * stride as isize),
                width as usize * 4,
            )
        };
        for x in 0..width as usize {
            let at = (y * width as usize + x) * 3;
            rgb[at] = row[x * 4 + 2];
            rgb[at + 1] = row[x * 4 + 1];
            rgb[at + 2] = row[x * 4];
        }
    }
    Ok(rgb)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn macroblock_padding_is_removed_from_preview() {
        let pixels = vec![1; 336 * 304 * 3];
        let cropped = crop_rgb(pixels, 336, 304, (0, 0, 326, 296));
        assert_eq!(cropped.len(), 326 * 296 * 3);
        assert!(cropped.iter().all(|value| *value == 1));
    }
    #[test]
    fn rgb_rows_respect_padding_and_negative_stride() {
        let bytes = [
            0, 0, 255, 255, 0, 255, 0, 255, 7, 7, 7, 7, 255, 0, 0, 255, 255, 255, 255, 255, 7, 7,
            7, 7,
        ];
        unsafe {
            assert_eq!(
                rows_to_rgb(bytes.as_ptr(), 12, 2, 2).unwrap(),
                [255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255]
            );
            assert_eq!(
                rows_to_rgb(bytes.as_ptr().add(12), -12, 2, 2).unwrap(),
                [0, 0, 255, 255, 255, 255, 255, 0, 0, 0, 255, 0]
            );
        }
    }
    #[test]
    fn invalid_requests_are_rejected_without_opening_media() {
        assert!(run(Path::new("missing.mp4"), &[f64::NAN], Path::new("unused")).is_err());
        assert!(run(Path::new("missing.mp4"), &[0.0; 9], Path::new("unused")).is_err());
    }
}
