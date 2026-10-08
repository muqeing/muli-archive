"""Disposable local decoder. Input is an already-open read-only descriptor."""
import io
import os
import resource
import sys


def decode(fd, raw_format):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 100_000_000
    path = ('/proc/self/fd/' if os.path.isdir('/proc/self/fd') else '/dev/fd/') + str(fd)
    label = '照片预览'
    if raw_format:
        import rawpy
        with rawpy.RawPy() as raw:
            # open_file does not unpack the full sensor image. Most cameras have
            # a JPEG preview, which is much cheaper than demosaicing the RAW.
            raw.open_file(path)
            try:
                thumb = raw.extract_thumb()
                image = Image.open(io.BytesIO(thumb.data)) if thumb.format == rawpy.ThumbFormat.JPEG else Image.fromarray(thumb.data)
                orientation = image.getexif().get(274)
                # Ask JPEG to downsample before loading/rotating a 61 MP preview.
                image.draft('RGB', (720, 720))
                image.thumbnail((720, 720), Image.Resampling.LANCZOS)
                image = ImageOps.exif_transpose(image)
                if orientation is None:
                    transform = {3: Image.Transpose.ROTATE_180, 5: Image.Transpose.ROTATE_90, 6: Image.Transpose.ROTATE_270}.get(raw.sizes.flip)
                    if transform is not None:
                        image = image.transpose(transform)
                label = 'RAW 内嵌预览'
            except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError, OSError, ValueError):
                raw.unpack()
                image = Image.fromarray(raw.postprocess(half_size=True, use_camera_wb=True, output_bps=8))
                label = 'RAW 解码预览'
    else:
        with Image.open(path) as source:
            source.draft('RGB', (720, 720))
            source.thumbnail((720, 720), Image.Resampling.LANCZOS)
            image = ImageOps.exif_transpose(source)
    image.thumbnail((720, 720), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.convert('RGB').save(output, format='JPEG', quality=82, optimize=True)
    data = output.getvalue()
    if not 4 <= len(data) <= 512 * 1024:
        raise ValueError('preview_size_invalid')
    return label, data


def main():
    # Bound malformed/unsupported files without affecting the backup process.
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    if sys.platform == 'linux':
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    os.nice(10)
    try:
        label, data = decode(int(sys.argv[1]), sys.argv[2] == 'raw')
        sys.stdout.buffer.write(label.encode() + b'\n' + data)
    except Exception:
        sys.exit(1)  # Never expose source paths or decoder internals in the UI.


if __name__ == '__main__':
    main()
