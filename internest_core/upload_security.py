"""Upload hardening (Phase 7): content signatures, random storage names, optional malware scanning.

    validate_file_signature   the first bytes must match the extension (a renamed .exe is not a .pdf)
    scan_upload               ClamAV INSTREAM when CLAMAV_HOST is configured; a no-op otherwise (PythonAnywhere has no clamd)
    *_upload_to               private files get random names: no student name or guessable path in a URL

Only NEW uploads are checked: model validators also run on files already stored (every profile or admin save), and
re-reading or re-scanning those would block unrelated edits (independent review, finding 1).
Size limits stay in models.validate_max_file_size (an oversized file is not read or scanned here); private files are
served only through serve_protected_media.
"""
import os
import socket
import struct
import uuid
import zipfile

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models.fields.files import FieldFile
from django.utils.deconstruct import deconstructible
from django.utils.translation import gettext_lazy as _

ZIP_MAGIC = b"PK\x03\x04"
SIGNATURES = {
    "pdf": (b"%PDF-",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "webp": (b"RIFF",),  # + "WEBP" at offset 8, checked below
    "doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    "docx": (ZIP_MAGIC,),
}
HEAD = 16
MAX_CHECKED_BYTES = 5 * 1024 * 1024  # = models.MAX_UPLOAD_SIZE; bigger files are rejected by the size validator


def _is_new_upload(file) -> bool:
    if not file:
        return False
    if isinstance(file, FieldFile) and getattr(file, "_committed", True):
        return False  # already stored: checked when it was uploaded
    return (getattr(file, "size", 0) or 0) <= MAX_CHECKED_BYTES


def _read_head(file, n=HEAD):
    pos = file.tell() if hasattr(file, "tell") else 0
    try:
        file.seek(0)
        return file.read(n)
    finally:
        file.seek(pos)


def validate_file_signature(file):
    """Reject a file whose content does not match its extension. Unknown extensions are left to the extension validator."""
    if not _is_new_upload(file):
        return
    ext = os.path.splitext(getattr(file, "name", "") or "")[1].lower().lstrip(".")
    expected = SIGNATURES.get(ext)
    if expected is None:
        return
    head = _read_head(file)
    ok = any(head.startswith(sig) for sig in expected)
    if ext == "webp":
        ok = ok and head[8:12] == b"WEBP"
    if ext == "docx" and ok:  # a zip is not necessarily a Word file
        pos = file.tell()
        try:
            file.seek(0)
            with zipfile.ZipFile(file) as archive:  # reads only the central directory, not every entry
                ok = "word/document.xml" in archive.namelist()
        except (zipfile.BadZipFile, OSError, ValueError):
            ok = False
        finally:
            file.seek(pos)
    if not ok:
        raise ValidationError(_("This file's content doesn't match its type. Upload a real %(ext)s file.") % {"ext": ext.upper()})


def scan_upload(file):
    """Ask ClamAV (clamd INSTREAM) about the file when CLAMAV_HOST is set. Fails closed when the scanner is configured
    but unreachable, so an outage cannot let unscanned files through."""
    host = getattr(settings, "CLAMAV_HOST", "")
    if not host or not _is_new_upload(file):
        return
    port = int(getattr(settings, "CLAMAV_PORT", 3310))
    pos = file.tell()
    try:
        file.seek(0)
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.sendall(b"zINSTREAM\0")
            while chunk := file.read(64 * 1024):
                sock.sendall(struct.pack("!L", len(chunk)) + chunk)
            sock.sendall(struct.pack("!L", 0))
            reply = sock.recv(4096).decode("utf-8", "replace")
    except OSError:
        raise ValidationError(_("We couldn't scan this file right now. Please try again in a few minutes."))
    finally:
        file.seek(pos)
    reply = reply.strip("\0 \r\n")
    if reply.endswith("FOUND"):
        raise ValidationError(_("This file was rejected by our malware scanner."))
    if not reply.endswith(" OK"):  # "... ERROR", size limit, empty reply: never treat an unclear answer as clean
        raise ValidationError(_("We couldn't scan this file right now. Please try again in a few minutes."))


UPLOAD_VALIDATORS = [validate_file_signature, scan_upload]


@deconstructible
class RandomName:
    """upload_to that keeps the folder and extension but replaces the name with a random one."""

    def __init__(self, folder):
        self.folder = folder.rstrip("/")

    def __call__(self, instance, filename):
        ext = os.path.splitext(filename)[1].lower()[:10]
        return f"{self.folder}/{uuid.uuid4().hex}{ext}"

    def __eq__(self, other):
        return isinstance(other, RandomName) and other.folder == self.folder
