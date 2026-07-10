"""winutil/_job.py -- tie spawned helper lifetimes to the ST process.

Problem
-------
ST's plugin_unloaded() fires on BOTH plugin reload and ST quit. It can't tell
them apart. Long-lived helpers (ai_sdk bridge, Flask search app, PyBackup app)
therefore can't be safely killed in plugin_unloaded: doing so would wipe the
bridge's conversation history on every reload. But NOT killing them orphans
detached python.exe processes that survive ST and have to be mopped up by hand
in Task Manager.

Solution
--------
A single Windows Job Object created with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
Every spawned helper pid is assigned to it. The job handle is stashed on
`sys` (which ST's plugin reload leaves alone), so it survives reloads intact
-- the bridge helper stays alive across reloads, as ai_sdk expects. When ST
exits -- cleanly OR via Task Manager kill -- Windows closes the handle as part
of process teardown, and the kernel reaps every assigned child. No
plugin_unloaded participation required; the OS does it.

On non-Windows this module is a no-op so the rest of the plugin keeps
importing cross-platform.
"""

import sys

try:
    import ctypes
    from ctypes import wintypes
    _HAVE_WIN = True
except Exception:
    _HAVE_WIN = False

if _HAVE_WIN:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
    ]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL


def _create_job():
    h = _k32.CreateJobObjectW(None, None)
    if not h:
        raise OSError("CreateJobObjectW failed: %d" % ctypes.get_last_error())
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _k32.SetInformationJobObject(
        h,
        _JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        err = ctypes.get_last_error()
        _k32.CloseHandle(h)
        raise OSError("SetInformationJobObject failed: %d" % err)
    return h


def _get_job():
    """Return the process-wide job handle, creating it on first call.

    Stashed on `sys` so plugin reloads (which re-execute this module but
    leave the `sys` module object alone) reuse the existing handle rather
    than creating a new one -- the latter would release the prior handle
    and prematurely kill the bridge that ai_sdk deliberately keeps alive
    across reloads.
    """
    if not _HAVE_WIN:
        return None
    handle = getattr(sys, "_st_win_job_handle", None)
    if handle is None:
        try:
            handle = _create_job()
            sys._st_win_job_handle = handle
        except OSError as e:
            print("winutil._job: job setup failed: %s" % e)
            sys._st_win_job_handle = False  # negative cache; don't retry
            return None
    if handle is False:
        return None
    return handle


def assign_pid(pid):
    """Assign a running child pid to the ST-lifetime job.

    Best-effort: on Windows 8+ a process can be a member of multiple jobs,
    so this works even if the child already joined one. Pre-Win8 it would
    fail if the child were already in a job; we just log-and-continue.
    No-op off-Windows.
    """
    if not _HAVE_WIN:
        return
    job = _get_job()
    if job is None:
        return
    hproc = _k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not hproc:
        return
    try:
        if not _k32.AssignProcessToJobObject(job, hproc):
            # Not fatal: child still runs, just isn't auto-reaped on ST exit.
            pass
    finally:
        _k32.CloseHandle(hproc)