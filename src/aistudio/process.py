"""Own a helper and its descendants without touching other running services."""

import ctypes
import os
import signal
import subprocess
from pathlib import Path


class OwnedProcess:
    def __init__(self, args, *, cwd: Path, env: dict, log: Path):
        self._job = None
        self._stopped = False
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as output:
            self.process = subprocess.Popen(
                args, cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                stdout=output, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
        if os.name == "nt":
            try:
                self._job = _windows_job(self.process)
            except Exception:
                self.process.kill()
                self.process.wait()
                raise

    @property
    def pid(self):
        return self.process.pid

    def poll(self):
        return self.process.poll()

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        # Closing the job also kills child browsers left behind by a crashed
        # helper. A job handle is owned by this process, never a remembered PID.
        if self._job:
            ctypes.windll.kernel32.CloseHandle(self._job)
            self._job = None
        elif os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                self.process.kill()
            self.process.wait(timeout=3)
        # The helper may have exited before its browser, including after a crash.
        # Its session was created by us; never leave those descendants running.
        if os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.process.stdin:
            self.process.stdin.close()


def _windows_job(process):
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters)] + [
            (name, ctypes.c_size_t) for name in (
                "ProcessMemoryLimit", "JobMemoryLimit", "PeakProcessMemoryUsed", "PeakJobMemoryUsed",
            )
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimit()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        kernel.CloseHandle(job)
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
        kernel.CloseHandle(job)
        raise ctypes.WinError(ctypes.get_last_error())
    # Return a typed handle so CloseHandle never truncates it on 64-bit Windows.
    return wintypes.HANDLE(job)
