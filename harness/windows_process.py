"""Run a Windows command inside a job that owns its entire process tree."""

import ctypes
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

from .workflow import MAX_OUTPUT_BYTES, ActionError


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", BasicLimits),
        ("IoInfo", IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
]
kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100


def _job_handle():
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits),
    ):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(job)
        raise error
    return job


def _assign(job, pid: int) -> None:
    process = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel32.AssignProcessToJobObject(job, process):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(process)


def _read_output(fd: int, result: queue.Queue) -> None:
    output = bytearray()
    try:
        while chunk := os.read(fd, 65536):
            output.extend(chunk)
            if len(output) > MAX_OUTPUT_BYTES:
                raise ActionError("process_output_too_large")
    except (OSError, ActionError) as error:
        result.put(error)
    else:
        result.put(bytes(output))


def run_windows_process(
    argv: list[str], cwd: Path, timeout: float, input_bytes: bytes,
) -> bytes:
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="harness-process-") as directory:
        root = Path(directory)
        (root / "request.json").write_text(json.dumps([argv, str(cwd)]), encoding="utf-8")
        (root / "stdin").write_bytes(input_bytes)
        try:
            job = _job_handle()
        except OSError as error:
            raise ActionError("process_start_failed", retryable=False) from error

        process = None
        reader = None
        assigned = False
        try:
            try:
                process = subprocess.Popen(
                    [sys.executable, "-B", str(Path(__file__).with_name("windows_worker.py")), str(root)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
                _assign(job, process.pid)
                assigned = True
                assert process.stdin is not None and process.stdout is not None
                with process.stdin:
                    process.stdin.write(b"1")
            except OSError as error:
                raise ActionError("process_start_failed", retryable=False) from error

            result: queue.Queue = queue.Queue(maxsize=1)
            reader = threading.Thread(target=_read_output, args=(process.stdout.fileno(), result))
            reader.start()
            try:
                output = result.get(timeout=max(0, deadline - time.monotonic()))
                if isinstance(output, BaseException):
                    raise output
                exit_code = process.wait(timeout=max(0, deadline - time.monotonic()))
            except (queue.Empty, subprocess.TimeoutExpired) as error:
                raise ActionError("timeout") from error
            if (root / "start-failed").exists():
                raise ActionError("process_start_failed", retryable=False)
            if exit_code != 0:
                raise ActionError("process_nonzero_exit", exit_code=exit_code)
            return output
        finally:
            kernel32.CloseHandle(job)
            if process is not None:
                if not assigned and process.poll() is None:
                    process.kill()
                if process.stdin is not None:
                    process.stdin.close()
                process.wait(timeout=5)
                if reader is not None:
                    reader.join()
                if process.stdout is not None:
                    process.stdout.close()
