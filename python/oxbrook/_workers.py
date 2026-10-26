"""Choosing how many Python worker loops to start.

Measured on 2026-09-06 (`bench/sweep.py`, handler CPU cost against loop count):

* On a GIL build, one loop is best or tied at every handler cost, and extra
  loops only cost throughput. So: one loop, always.
* On a free-threaded build, one loop is *never* best. Even a handler that does
  nothing gains from more loops, and a handler doing 500us of work gains 3.4x.
  Throughput peaks at the performance-core count and falls off when the
  efficiency cores get oversubscribed.

So the target is "how many cores can actually run Python in parallel", which is
not `os.cpu_count()`. That number counts efficiency cores, and inside a
container it reports the host's cores rather than the cgroup limit, which would
start dozens of loops for a two-CPU quota.

Every probe below is best-effort and returns None when it cannot answer. The
final answer is the most constrained of everything that did answer.
"""

import os
import struct
import sys

# Guard against a probe returning something absurd, not a tuning limit. Raise it
# with `workers=` for a CPU-heavy service on a large homogeneous machine.
MAX_DEFAULT_WORKERS = 8


def gil_enabled() -> bool:
    probe = getattr(sys, "_is_gil_enabled", None)
    return True if probe is None else bool(probe())


def _sysctl_int(name: str) -> int | None:
    """Read an integer sysctl on macOS or BSD without shelling out."""
    if not sys.platform.startswith(("darwin", "freebsd")):
        return None
    try:
        import ctypes
        import ctypes.util

        lib = ctypes.util.find_library("c")
        if lib is None:
            return None
        libc = ctypes.CDLL(lib, use_errno=True)
        value = ctypes.c_int64(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        rc = libc.sysctlbyname(name.encode(), ctypes.byref(value), ctypes.byref(size), None, 0)
        return int(value.value) if rc == 0 and value.value > 0 else None
    except Exception:  # noqa: BLE001 - a probe that raises must answer None, not propagate
        return None


def _linux_performance_cores() -> int | None:
    """The fast half of a hybrid Linux machine, if it says it has one.

    Two kernels expose it differently and neither exists on a uniform machine,
    which is the common case and correctly answers None:

    * Intel hybrid parts list their P-cores in `/sys/devices/cpu_core/cpus`,
      as a range list like `0-15,32-47`.
    * ARM big.LITTLE gives every CPU a `cpu_capacity`; the big ones are those
      at the maximum.
    """
    try:
        with open("/sys/devices/cpu_core/cpus") as handle:
            listed = handle.read().strip()
        if listed:
            total = 0
            for part in listed.split(","):
                if "-" in part:
                    low, high = part.split("-")
                    total += int(high) - int(low) + 1
                else:
                    total += 1
            return total or None
    except OSError:
        pass

    try:
        import glob

        capacities = []
        for path in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpu_capacity"):
            with open(path) as handle:
                capacities.append(int(handle.read().strip()))
        if capacities and len(set(capacities)) > 1:
            return capacities.count(max(capacities))
    except (OSError, ValueError):
        pass
    return None


#: `RelationProcessorCore`: one record per physical core.
_PROCESSOR_CORE = 0


def _windows_cores(records: bytes) -> tuple[int, int | None] | None:
    """(physical cores, performance cores) from what
    `GetLogicalProcessorInformationEx(RelationProcessorCore)` wrote.

    Each record is `Relationship` and `Size` (two DWORDs), then a
    PROCESSOR_RELATIONSHIP whose second byte is `EfficiencyClass`: higher is
    faster, and every core has the same class on a machine that is not
    hybrid, which then has no performance count to report.
    """
    classes: list[int] = []
    offset = 0
    while offset + 10 <= len(records):
        relationship, size = struct.unpack_from("<II", records, offset)
        if size < 10:
            return None
        if relationship == _PROCESSOR_CORE:
            classes.append(records[offset + 9])
        offset += size
    if not classes:
        return None
    fastest = max(classes)
    split = fastest != min(classes)
    return len(classes), classes.count(fastest) if split else None


def _windows_topology() -> tuple[int, int | None] | None:
    """Physical and performance cores on Windows, which `os.cpu_count()` gives
    only as logical processors: SMT siblings counted twice."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        call = kernel32.GetLogicalProcessorInformationEx
        call.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        call.restype = wintypes.BOOL
        length = wintypes.DWORD(0)
        # Asked with no buffer first, for the size it needs.
        call(_PROCESSOR_CORE, None, ctypes.byref(length))
        if not length.value:
            return None
        buffer = ctypes.create_string_buffer(length.value)
        if not call(_PROCESSOR_CORE, buffer, ctypes.byref(length)):
            return None
        return _windows_cores(buffer.raw[: length.value])
    except Exception:  # noqa: BLE001 - a probe that raises must answer None, not propagate
        return None


def _performance_cores() -> int | None:
    """Cores that run at full speed. Apple Silicon splits these from the
    efficiency cores, and the sweep showed loops on efficiency cores losing
    throughput rather than adding it. Linux and Windows hybrid machines split
    them too, and a uniform machine has no split to report."""
    macos = _sysctl_int("hw.perflevel0.physicalcpu")
    if macos:
        return macos
    windows = _windows_topology()
    if windows is not None:
        return windows[1]
    return _linux_performance_cores()


def _physical_cores() -> int | None:
    """Physical cores, ignoring SMT siblings. Two hyperthreads on one core do
    not give two loops' worth of parallel Python."""
    macos = _sysctl_int("hw.physicalcpu")
    if macos:
        return macos
    windows = _windows_topology()
    if windows is not None:
        return windows[0]
    try:
        import glob

        pairs = set()
        for path in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/topology"):
            try:
                with open(f"{path}/core_id") as f:
                    core = f.read().strip()
                with open(f"{path}/physical_package_id") as f:
                    package = f.read().strip()
                pairs.add((package, core))
            except OSError:
                continue
        return len(pairs) or None
    except Exception:  # noqa: BLE001 - a probe that raises must answer None, not propagate
        return None


def _cgroup_quota() -> int | None:
    """CPU quota of the current container, rounded up. `os.cpu_count()` does not
    see this, which is how a 2-CPU container on a 64-core host ends up starting
    64 event loops."""
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:  # cgroup v2
            quota, period = f.read().split()
            if quota != "max":
                return max(1, -(-int(quota) // int(period)))
            return None
    except (OSError, ValueError):
        pass
    try:
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") as f:  # cgroup v1
            quota = int(f.read().strip())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as f:
            period = int(f.read().strip())
        if quota > 0 and period > 0:
            return max(1, -(-quota // period))
    except (OSError, ValueError):
        pass
    return None


def _available_cpus() -> int:
    """Respects CPU affinity where the platform reports it."""
    counter = getattr(os, "process_cpu_count", None)
    return (counter() if counter else None) or os.cpu_count() or 1


def detect_parallelism() -> int:
    """How many loops can genuinely run Python at the same time."""
    limits = [
        limit
        for limit in (_performance_cores(), _physical_cores(), _cgroup_quota())
        if limit
    ]
    limits.append(_available_cpus())
    return max(1, min(limits))


def default_workers() -> int:
    if gil_enabled():
        return 1
    return max(1, min(MAX_DEFAULT_WORKERS, detect_parallelism()))


def describe() -> dict[str, int | None]:
    """Everything the probes found. For diagnostics and tests."""
    return {
        "performance_cores": _performance_cores(),
        "physical_cores": _physical_cores(),
        "cgroup_quota": _cgroup_quota(),
        "available_cpus": _available_cpus(),
        "os_cpu_count": os.cpu_count(),
        "detected": detect_parallelism(),
        "default_workers": default_workers(),
    }
