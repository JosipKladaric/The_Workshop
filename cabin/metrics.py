"""Optional local resource measurements, with no third-party dependencies."""
import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from .store import now


def sample_resources():
    result = dict(timestamp=now(), ram_total_mb=None, ram_available_mb=None,
                  process_ram_mb=None, gpu=[])
    try:
        if os.name == 'nt':
            class MemoryStatus(ctypes.Structure):
                _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
                    (k, ctypes.c_ulonglong) for k in ('total_physical', 'available_physical',
                        'total_page', 'available_page', 'total_virtual', 'available_virtual', 'extended')]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                result['ram_total_mb'] = round(status.total_physical / 1048576, 1)
                result['ram_available_mb'] = round(status.available_physical / 1048576, 1)
            class ProcessMemory(ctypes.Structure):
                _fields_ = [('size', ctypes.c_ulong), ('faults', ctypes.c_ulong)] + [
                    (k, ctypes.c_size_t) for k in ('peak_working', 'working', 'quota_peak_paged',
                        'quota_paged', 'quota_peak_nonpaged', 'quota_nonpaged', 'pagefile', 'peak_pagefile')]
            memory = ProcessMemory()
            memory.size = ctypes.sizeof(memory)
            kernel = ctypes.windll.kernel32
            kernel.GetCurrentProcess.restype = ctypes.c_void_p
            psapi = ctypes.windll.psapi
            psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
            if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(memory), memory.size):
                result['process_ram_mb'] = round(memory.working / 1048576, 1)
        elif Path('/proc/meminfo').exists():
            memory = {line.split(':')[0]: int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
            result['ram_total_mb'] = round(memory['MemTotal'] / 1024, 1)
            result['ram_available_mb'] = round(memory['MemAvailable'] / 1024, 1)
            for line in Path('/proc/self/status').read_text().splitlines():
                if line.startswith('VmRSS:'):
                    result['process_ram_mb'] = round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, AttributeError):
        pass
    if shutil.which('nvidia-smi'):
        try:
            flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
            completed = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.used,memory.total,temperature.gpu,power.draw',
                                        '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=3, **flags)
            for line in completed.stdout.splitlines():
                parts = [p.strip() for p in line.split(',')]
                if len(parts) == 5:
                    row = {'name': parts[0]}
                    for key, value in zip(('memory_used_mb', 'memory_total_mb', 'temperature_c', 'power_w'), parts[1:]):
                        try:
                            row[key] = float(value)
                        except ValueError:
                            row[key] = None
                    result['gpu'].append(row)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return result


class ResourceMonitor:
    """Sample device-wide GPU/RAM, not a claim of per-model attribution."""
    def __init__(self, interval=2):
        self.interval = interval
        self.peak = {}
        self.stop = threading.Event()
        self.thread = None

    def observe(self):
        data = sample_resources()
        self.peak['samples'] = self.peak.get('samples', 0) + 1
        self.peak['scope'] = 'host/device totals; includes other applications'
        for key in ('process_ram_mb',):
            if data.get(key) is not None:
                self.peak[key] = max(self.peak.get(key, 0), data[key])
        if data.get('ram_total_mb') is not None and data.get('ram_available_mb') is not None:
            self.peak['host_ram_used_mb'] = max(self.peak.get('host_ram_used_mb', 0), data['ram_total_mb'] - data['ram_available_mb'])
        for gpu in data['gpu']:
            for source, target in [('memory_used_mb', 'gpu_memory_mb'), ('temperature_c', 'gpu_temperature_c'), ('power_w', 'gpu_power_w')]:
                if gpu.get(source) is not None:
                    self.peak[target] = max(self.peak.get(target, 0), gpu[source])

    def _loop(self):
        while not self.stop.wait(self.interval):
            self.observe()

    def __enter__(self):
        self.observe()
        self.thread = threading.Thread(target=self._loop, name='resource-sampler', daemon=True)
        self.thread.start()
        return self

    def snapshot(self):
        return dict(self.peak)

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=5)
        self.observe()
