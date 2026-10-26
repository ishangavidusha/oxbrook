#!/usr/bin/env python3
"""Sanity checks for worker-count detection.

Cannot assert exact numbers, since they are machine specific. Asserts the
invariants that would actually break a deployment.
"""
import struct
import sys

from oxbrook import _workers


def record(relationship: int, efficiency: int, size: int = 48) -> bytes:
    """One SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX record, as Windows writes it."""
    head = struct.pack("<IIBB", relationship, size, 0, efficiency)
    return head + bytes(size - len(head))


def windows_records_are_read() -> list[str]:
    """The Windows topology parser, on records built here: it runs on every
    platform, while the call that produces them runs only on Windows CI."""
    failures = []
    cases = [
        ("four uniform cores", b"".join(record(0, 0) for _ in range(4)), (4, None)),
        ("hybrid: 6 fast, 8 efficient",
         b"".join([record(0, 1)] * 6 + [record(0, 0)] * 8), (14, 6)),
        ("other relationships skipped",
         record(2, 7, 80) + record(0, 0) + record(1, 9, 64) + record(0, 0), (2, None)),
        ("nothing", b"", None),
        ("a record claiming no size", struct.pack("<II", 0, 0) + bytes(40), None),
    ]
    for label, data, expected in cases:
        got = _workers._windows_cores(data)
        if got != expected:
            failures.append(f"windows records, {label}: {got} rather than {expected}")
    return failures


def main() -> None:
    info = _workers.describe()
    gil = _workers.gil_enabled()

    print(f"build             : {'GIL' if gil else 'free-threaded'}")
    for key, value in info.items():
        print(f"{key:<18}: {value}")

    failures = windows_records_are_read()
    workers = info["default_workers"]

    if gil and workers != 1:
        failures.append(f"GIL build must use exactly 1 loop, got {workers}")
    if workers < 1:
        failures.append(f"worker count must be at least 1, got {workers}")
    if workers > _workers.MAX_DEFAULT_WORKERS:
        failures.append(f"{workers} exceeds the cap of {_workers.MAX_DEFAULT_WORKERS}")
    if info["detected"] > info["available_cpus"]:
        failures.append(
            f"detected {info['detected']} exceeds {info['available_cpus']} available CPUs"
        )
    if not gil and workers > info["detected"]:
        failures.append(f"{workers} loops exceeds detected parallelism {info['detected']}")
    if sys.platform == "win32":
        # The real call, on the one platform that has it: an answer, and one
        # no larger than the logical processors it groups.
        physical = info["physical_cores"]
        if not physical or physical > (info["os_cpu_count"] or 0):
            failures.append(f"Windows reported {physical} physical cores "
                            f"for {info['os_cpu_count']} logical processors")
    perf = info["performance_cores"]
    if perf and info["detected"] > perf:
        failures.append(f"detected {info['detected']} exceeds {perf} performance cores")

    print("\nRESULT:", "FAIL - " + "; ".join(failures) if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
