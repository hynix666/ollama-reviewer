"""Process liveness, one home for "is this pid alive?".

dashboard's pidfile lock and selftest's mutation-marker recovery share it.
selftest asks before its quiescence gate - before any project module may be
imported - so this module must stay standard-library only and outside the
mutation registry: importing it mid-mutation is safe by construction.
"""

import os


def pid_alive(pid):
    """Does pid exist? Never signals it: os.kill(pid, 0) terminates on Windows."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # EPERM: the process exists
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if h:
        k.CloseHandle(h)
        return True
    return ctypes.GetLastError() == 5  # ERROR_ACCESS_DENIED: exists, not ours
