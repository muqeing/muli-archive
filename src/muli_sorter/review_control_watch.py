"""Invalidate presentation only on control-record writes, never media reads.

Linux inotify avoids a periodic per-receipt stat sweep. Overflow or watch loss
invalidates the display as well. Non-Linux uses atomic directory signatures;
explicit full history/source checks remain available on all platforms.
"""
import ctypes
import os
from pathlib import Path
import select
import struct
import sys
import threading


class ControlWatch:
    def __init__(self, paths, invalidate):
        self.paths = [Path(p) for p in paths]
        self.invalidate = invalidate
        self.stop = threading.Event()
        self.fd = -1
        self.thread = None
        if sys.platform != 'linux':
            return
        libc = ctypes.CDLL(None, use_errno=True)
        init = libc.inotify_init1
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        self.fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), 'Cannot watch archive display controls')
        self.add = libc.inotify_add_watch
        self.add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.add.restype = ctypes.c_int
        self.watches = {}
        self.refresh_watches()
        self.thread = threading.Thread(target=self.run, name='review-control-watch', daemon=True)
        self.thread.start()

    def refresh_watches(self):
        # Fixed flat control directories only; no media paths or recursive scan.
        for path in self.paths:
            if path in self.watches:
                continue
            if path.is_dir() and not path.is_symlink():
                wd = self.add(self.fd, os.fsencode(path),
                    0x2 | 0x4 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800)
                if wd < 0:
                    raise OSError(ctypes.get_errno(), 'Cannot watch archive controls')
                self.watches[path] = wd

    def run(self):
        try:
            while not self.stop.is_set():
                self.refresh_watches()
                if not select.select([self.fd], [], [], 1)[0]:
                    continue
                data = os.read(self.fd, 65536)
                offset, changed = 0, False
                while offset + 16 <= len(data):
                    wd, mask, cookie, length = struct.unpack_from('iIII', data, offset)
                    name = data[offset + 16:offset + 16 + length].split(b'\0', 1)[0]
                    offset += 16 + length
                    # Control writers may have non-JSON locks: only JSON/control
                    # directory events matter; overflow cannot be ignored.
                    if not name or name.endswith(b'.json') or mask & 0x4000:
                        changed = True
                    if mask & (0x400 | 0x800 | 0x8000):
                        self.watches = {p: w for p, w in self.watches.items() if w != wd}
                if changed:
                    self.invalidate()
        except (OSError, ValueError):
            if not self.stop.is_set():
                self.invalidate()
        finally:
            if self.fd >= 0:
                os.close(self.fd)
                self.fd = -1

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(2)
