"""Cross-platform process singleton locks."""

import ctypes
import os


class ProcessLock:
    """Acquire an inter-process lock that is released when the owner exits."""

    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.handle = None
        self.file_handle = None

    def acquire(self):
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_bool,
                ctypes.c_wchar_p,
            ]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            ctypes.set_last_error(0)
            self.handle = kernel32.CreateMutexW(None, False, self.name)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            if ctypes.get_last_error() == 183:
                kernel32.CloseHandle(self.handle)
                self.handle = None
                return False
            return True

        import fcntl

        self.file_handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self.file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            self.file_handle.close()
            self.file_handle = None
            return False
        return True

    def release(self):
        if self.handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle(self.handle)
            self.handle = None
        if self.file_handle is not None:
            import fcntl

            try:
                fcntl.flock(self.file_handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.file_handle.close()
                self.file_handle = None
