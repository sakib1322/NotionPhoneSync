"""Keeps the app small: copies of cloud files you opened (and previews) are kept within
a chosen limit (least recently used go first) and can be cleared. Your own files on the
laptop are never touched."""
import os
import time

import core
from cloud import CACHE_DIR

THUMB_LIMIT = 300 * 1024 * 1024


def _files(d):
    for base, _, names in os.walk(d):
        for n in names:
            if not n.endswith(".part"):
                yield os.path.join(base, n)


def size_of(d):
    total = 0
    for p in _files(d):
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def thumbs_dir():
    return os.path.join(CACHE_DIR, "thumbs")


def usage():
    """(downloaded files, previews, app data) in bytes."""
    total = size_of(CACHE_DIR)
    th = size_of(thumbs_dir())
    data = size_of(core.DATA_DIR)
    return total - th, th, data


def trim(d, limit, skip=None):
    """Removes least recently used files until the folder is under the limit (files used in the last 10 minutes stay)."""
    recent = time.time() - 600
    items = []
    for p in _files(d):
        if skip and os.path.commonpath([p, skip]) == skip:
            continue
        try:
            st = os.stat(p)
            items.append((st.st_atime if st.st_atime > st.st_mtime else st.st_mtime, st.st_size, p))
        except OSError:
            pass
    total = sum(s for _, s, _ in items)
    for t, s, p in sorted(items):
        if total <= limit:
            break
        if t > recent:
            continue
        try:
            os.remove(p)
            total -= s
        except OSError:
            pass


def trim_all(settings):
    trim(CACHE_DIR, int(settings.get("cache_limit_mb") or 1024) * 1024 * 1024, skip=thumbs_dir())
    trim(thumbs_dir(), THUMB_LIMIT)


def clear_downloads():
    for p in list(_files(CACHE_DIR)):
        try:
            os.remove(p)
        except OSError:
            pass


def downloads():
    out = []
    for p in _files(CACHE_DIR):
        if os.path.commonpath([p, thumbs_dir()]) == thumbs_dir():
            continue
        try:
            out.append((os.path.getsize(p), p))
        except OSError:
            pass
    return sorted(out, reverse=True)


def remove_download(block):
    """Removes the downloaded copy of one cloud file (the cloud copy and your own files are untouched)."""
    import shutil
    d = os.path.join(CACHE_DIR, block)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False


def is_downloaded(block):
    d = os.path.join(CACHE_DIR, block)
    return os.path.isdir(d) and any(not n.endswith(".part") for n in os.listdir(d))
