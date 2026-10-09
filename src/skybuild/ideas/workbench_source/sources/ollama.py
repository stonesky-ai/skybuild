"""The model endpoint sampler: what the endpoint holds and the GPU behind it, as the terminal view shows it.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from .. import hub as sv

# ---------------------------------------------------------------- ollama

@dataclass
class OllamaRow:
    label: str
    resident: int
    models: str
    busy: bool
    cpu_pct: float


@dataclass
class GpuInfo:
    util: int
    used: int
    total: int


class OllamaSampler:
    """Ollama instances and whether each is actually working.

    "Busy" is measured, not guessed: a resident model sits in memory on a
    keep-alive long after it stops answering, so residency alone reads as busy
    when nothing is happening. CPU time consumed between two refreshes is what
    separates serving from merely loaded.
    """

    def __init__(self) -> None:
        self._cpu: dict[int, tuple[float, float]] = {}
        self._containers: dict[str, str] = {}
        self._containers_at = 0.0

    def read(self, now: float, prime: bool = False) -> tuple[list[OllamaRow], GpuInfo | None]:
        """`prime` takes a throwaway first sample so the very first frame can
        still say whether ollama is working — a lone sample measures nothing."""
        if prime and not self._cpu:
            self.read(now)
            time.sleep(0.4)
            now = time.time()
        servers: dict[int, list[int]] = {}
        parents: dict[int, int] = {}
        cmds: dict[int, str] = {}
        for pid, cmd in sv.iter_processes():
            cmds[pid] = cmd
            fields = sv._stat_fields(pid)
            if fields:
                try:
                    parents[pid] = int(fields[1])
                except (IndexError, ValueError):
                    pass
        for pid, cmd in cmds.items():
            if re.search(r"ollama\s+serve\b", cmd):
                servers.setdefault(pid, [])
        for pid, cmd in cmds.items():
            if "llama-server" not in cmd:
                continue
            owner = parents.get(pid)
            if owner in servers:
                servers[owner].append(pid)

        rows: list[OllamaRow] = []
        for server_pid, workers in sorted(servers.items()):
            pct = sum(self._cpu_percent(w, now) for w in workers)
            pct += self._cpu_percent(server_pid, now)
            label, container = self._label_for(server_pid, now)
            models = self._models(container) if workers else ""
            rows.append(OllamaRow(label, len(workers), models, pct > 5.0, pct))
        return rows, self._gpu()

    def _cpu_percent(self, pid: int, now: float) -> float:
        cpu = sv._proc_cpu_seconds(pid)
        if cpu is None:
            self._cpu.pop(pid, None)
            return 0.0
        prior = self._cpu.get(pid)
        self._cpu[pid] = (cpu, now)
        if not prior or now <= prior[1]:
            return 0.0
        return 100.0 * (cpu - prior[0]) / (now - prior[1])

    def _label_for(self, pid: int, now: float) -> tuple[str, str]:
        """(compose project, container name) for a containerised ollama, else host."""
        try:
            cgroup = Path(f"/proc/{pid}/cgroup").read_text()
        except OSError:
            return "host", ""
        m = re.search(r"([0-9a-f]{64})", cgroup)
        if not m:
            return "host", ""
        cid = m.group(1)[:12]
        if now - self._containers_at > 30:
            self._containers = {}
            out = sv._run(["docker", "ps", "--no-trunc", "--format",
                        "{{.ID}}\t{{.Label \"com.docker.compose.project\"}}\t{{.Names}}"],
                       timeout=6.0)
            for line in out.splitlines():
                bits = line.split("\t")
                if bits and bits[0]:
                    self._containers[bits[0][:12]] = (bits[1] or bits[2] or "?") + "|" + (bits[2] or "")
            self._containers_at = now
        entry = self._containers.get(cid)
        if not entry:
            return "container", ""
        project, _, name = entry.partition("|")
        return project, name

    def _models(self, container: str) -> str:
        """`ollama ps` names what is resident and where it runs (GPU vs CPU)."""
        cmd = ["docker", "exec", container, "ollama", "ps"] if container else ["ollama", "ps"]
        out = sv._run(cmd, timeout=6.0)
        names, processor = [], ""
        for line in out.splitlines()[1:]:
            bits = line.split()
            if not bits:
                continue
            names.append(bits[0])
            if "GPU" in line:
                processor = "GPU"
            elif "CPU" in line:
                processor = "CPU"
        if not names:
            return ""
        shown = ", ".join(names[:3])
        return f"{shown} ({processor})" if processor else shown

    def _gpu(self) -> GpuInfo | None:
        out = sv._run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits"], timeout=6.0)
        line = out.strip().splitlines()[0] if out.strip() else ""
        bits = [b.strip() for b in line.split(",")]
        if len(bits) < 3:
            return None
        try:
            return GpuInfo(int(bits[0]), int(bits[1]), int(bits[2]))
        except ValueError:
            return None
