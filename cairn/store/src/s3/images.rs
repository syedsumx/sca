//! Image resizing on the file API: `GET /-/files/<bucket>/<key>?width=W&height=H[&fit=contain|cover|fill]`
//! returns the image scaled to fit (by default), cover or fill the box, in its own format.
//!
//! The read is the GetObject it stands for (authorized, audited, metered); only JPEG, PNG, GIF (its first
//! frame) and WebP are resized. Sources are limited (64 MiB, 50 megapixels) and decoding is bounded, so a
//! crafted image cannot exhaust memory; targets are limited to 4096 x 4096. The result carries a strong
//! ETag of its own and is not cached by the store (put a CDN or cache in front for hot thumbnails).

use std::io::Cursor;

use image::{DynamicImage, ImageFormat, ImageReader, Limits};

use super::errors::{self as e, ApiError};

pub const MAX_SOURCE_BYTES: usize = 64 << 20;
const MAX_SOURCE_PIXELS: u64 = 50_000_000;
const MAX_SIDE: u32 = 4096;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Fit {
    /// Scaled to fit inside the box, keeping its proportions.
    Contain,
    /// Scaled and cropped to fill the box exactly.
    Cover,
    /// Stretched to the box.
    Fill,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Resize {
    pub width: u32,
    pub height: u32,
    pub fit: Fit,
}

impl Resize {
    /// From the query's width, height and fit (None: no resize asked for).
    pub fn parse(width: Option<&str>, height: Option<&str>, fit: Option<&str>) -> Result<Option<Resize>, ApiError> {
        if width.is_none() && height.is_none() {
            return if fit.is_some() { Err(e::INVALID_ARGUMENT.clone()) } else { Ok(None) };
        }
        let side = |v: Option<&str>| -> Result<u32, ApiError> {
            match v {
                None => Ok(0),
                Some(s) => s.parse::<u32>().ok().filter(|n| (1..=MAX_SIDE).contains(n)).ok_or_else(|| e::INVALID_ARGUMENT.clone()),
            }
        };
        let (width, height) = (side(width)?, side(height)?);
        let fit = match fit.unwrap_or("contain") {
            "contain" => Fit::Contain,
            "cover" if width > 0 && height > 0 => Fit::Cover,
            "fill" if width > 0 && height > 0 => Fit::Fill,
            _ => return Err(e::INVALID_ARGUMENT.clone()),
        };
        Ok(Some(Resize { width, height, fit }))
    }

    /// A tag for the variant, appended to the source's ETag.
    pub fn tag(&self) -> String {
        format!(
            "{}x{}{}",
            self.width,
            self.height,
            match self.fit {
                Fit::Contain => "",
                Fit::Cover => "c",
                Fit::Fill => "f",
            }
        )
    }
}

/// The formats resized, by content type.
pub fn format_of(content_type: &str) -> Option<ImageFormat> {
    match content_type.split(';').next().unwrap_or("").trim().to_ascii_lowercase().as_str() {
        "image/jpeg" | "image/jpg" => Some(ImageFormat::Jpeg),
        "image/png" => Some(ImageFormat::Png),
        "image/gif" => Some(ImageFormat::Gif),
        "image/webp" => Some(ImageFormat::WebP),
        _ => None,
    }
}

/// Resizes an image; returns the new bytes (same format).
pub fn resize(src: &[u8], format: ImageFormat, r: Resize) -> Result<Vec<u8>, ApiError> {
    if src.len() > MAX_SOURCE_BYTES {
        return Err(e::IMAGE_TOO_LARGE.clone());
    }
    let mut reader = ImageReader::with_format(Cursor::new(src), format);
    let mut limits = Limits::default();
    limits.max_alloc = Some(512 << 20);
    limits.max_image_width = Some(20_000);
    limits.max_image_height = Some(20_000);
    reader.limits(limits);
    let (w, h) = reader.into_dimensions().map_err(|_| e::IMAGE_INVALID.clone())?;
    if w as u64 * h as u64 > MAX_SOURCE_PIXELS {
        return Err(e::IMAGE_TOO_LARGE.clone());
    }
    let mut reader = ImageReader::with_format(Cursor::new(src), format);
    let mut limits = Limits::default();
    limits.max_alloc = Some(512 << 20);
    reader.limits(limits);
    let img = reader.decode().map_err(|_| e::IMAGE_INVALID.clone())?;
    let (tw, th) = (if r.width == 0 { u32::MAX } else { r.width }, if r.height == 0 { u32::MAX } else { r.height });
    let filter = image::imageops::FilterType::Lanczos3;
    let out: DynamicImage = match r.fit {
        Fit::Contain if img.width() <= tw && img.height() <= th => img, // never enlarged
        Fit::Contain => img.resize(tw.min(MAX_SIDE), th.min(MAX_SIDE), filter),
        Fit::Cover => img.resize_to_fill(r.width, r.height, filter),
        Fit::Fill => img.resize_exact(r.width, r.height, filter),
    };
    let out = if format == ImageFormat::Jpeg { DynamicImage::ImageRgb8(out.to_rgb8()) } else { out };
    let mut buf = Cursor::new(Vec::new());
    out.write_to(&mut buf, format).map_err(|_| e::IMAGE_INVALID.clone())?;
    Ok(buf.into_inner())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_and_resize() {
        assert_eq!(Resize::parse(None, None, None).unwrap(), None);
        assert!(Resize::parse(Some("0"), None, None).is_err());
        assert!(Resize::parse(Some("5000"), None, None).is_err());
        assert!(Resize::parse(Some("10"), None, Some("cover")).is_err()); // cover needs both sides
        let r = Resize::parse(Some("40"), Some("30"), Some("cover")).unwrap().unwrap();
        let img = DynamicImage::ImageRgb8(image::RgbImage::from_fn(200, 100, |x, y| image::Rgb([x as u8, y as u8, 7])));
        let mut png = Cursor::new(Vec::new());
        img.write_to(&mut png, ImageFormat::Png).unwrap();
        let out = resize(png.get_ref(), ImageFormat::Png, r).unwrap();
        let back = image::load_from_memory_with_format(&out, ImageFormat::Png).unwrap();
        assert_eq!((back.width(), back.height()), (40, 30));
        let fit = resize(png.get_ref(), ImageFormat::Png, Resize::parse(Some("50"), None, None).unwrap().unwrap()).unwrap();
        let back = image::load_from_memory(&fit).unwrap();
        assert_eq!((back.width(), back.height()), (50, 25));
        assert!(resize(b"not an image", ImageFormat::Png, r).is_err());
        assert_eq!(format_of("image/jpeg; charset=x"), Some(ImageFormat::Jpeg));
        assert_eq!(format_of("text/plain"), None);
    }
}
