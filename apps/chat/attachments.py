"""Validation, size limits, and thumbnail generation for chat (and future workspace) files."""
from io import BytesIO

from django.core.files.base import ContentFile
from PIL import Image, ImageOps

# Extension → (file_type, allowed MIME prefixes/exact)
ALLOWED = {
    "jpg": ("image", {"image/jpeg"}),
    "jpeg": ("image", {"image/jpeg"}),
    "png": ("image", {"image/png"}),
    "webp": ("image", {"image/webp"}),
    "gif": ("image", {"image/gif"}),
    "mp4": ("video", {"video/mp4"}),
    "webm": ("video", {"video/webm"}),
    "mov": ("video", {"video/quicktime"}),
    "pdf": ("document", {"application/pdf"}),
    "doc": ("document", {"application/msword"}),
    "docx": ("document", {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}),
    "xls": ("document", {"application/vnd.ms-excel"}),
    "xlsx": ("document", {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),
    "ppt": ("document", {"application/vnd.ms-powerpoint"}),
    "pptx": ("document", {"application/vnd.openxmlformats-officedocument.presentationml.presentation"}),
    "txt": ("document", {"text/plain"}),
    "zip": ("archive", {"application/zip", "application/x-zip-compressed"}),
}

# Per-type size caps (bytes)
MAX_SIZE = {
    "image": 25 * 1024 * 1024,
    "video": 100 * 1024 * 1024,
    "document": 50 * 1024 * 1024,
    "archive": 50 * 1024 * 1024,
}

MAX_ATTACHMENTS = 10
THUMB_SIZE = 480


class AttachmentError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status


def _ext(name):
    if not name or "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def classify(upload):
    """Return (file_type, mime_type, original_name) or raise AttachmentError."""
    name = (getattr(upload, "name", None) or "file")[:255]
    ext = _ext(name)
    if ext not in ALLOWED:
        raise AttachmentError(
            "Unsupported file type. Allowed: images, videos, documents (pdf/office/txt), ZIP."
        )
    file_type, mimes = ALLOWED[ext]
    declared = (getattr(upload, "content_type", None) or "").split(";")[0].strip().lower()
    # Browsers sometimes send empty or generic types; trust extension then, else require a match.
    if declared and declared not in mimes and declared not in ("application/octet-stream", ""):
        # Still allow common near-matches for office files that vary by OS.
        if not (file_type == "document" and declared.startswith("application/")):
            if not (file_type == "image" and declared.startswith("image/")):
                if not (file_type == "video" and declared.startswith("video/")):
                    if not (file_type == "archive" and "zip" in declared):
                        raise AttachmentError(f"MIME type does not match extension for {name}")
    mime = declared if declared in mimes else next(iter(mimes))
    size = getattr(upload, "size", None)
    if size is None:
        raise AttachmentError("Could not determine file size")
    if size <= 0:
        raise AttachmentError(f"{name} is empty")
    if size > MAX_SIZE[file_type]:
        mb = MAX_SIZE[file_type] // (1024 * 1024)
        raise AttachmentError(f"{name} is too large (max {mb} MB for {file_type}s)")
    return file_type, mime, name


def make_thumbnail(upload, file_type):
    """Return a ContentFile JPEG thumbnail for images, else None. Does not seek the original permanently."""
    if file_type != "image":
        return None
    try:
        pos = upload.tell() if hasattr(upload, "tell") else None
        upload.seek(0)
        img = Image.open(upload)
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "P", "LA"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=82, optimize=True)
        if pos is not None:
            upload.seek(pos)
        else:
            upload.seek(0)
        return ContentFile(buf.getvalue(), name="thumb.jpg")
    except Exception:
        try:
            upload.seek(0)
        except Exception:
            pass
        return None
