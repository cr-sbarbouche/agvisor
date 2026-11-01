import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    """Resource limits, enforced by gVisor/cgroups on the machine hosting the sandbox.

    cpus    CPU cores, fractions allowed (0.5).
    memory  Hard RAM cap in docker syntax ("512m", "4g"). Swap is disabled.
    pids    Max processes/threads (stops fork bombs).
    shm     /dev/shm size ("1g"). Default: docker's 64m, or 1g for gui.
    """

    cpus: float = 2.0
    memory: str = "2g"
    pids: int = 512
    shm: str | None = None

    def __post_init__(self):
        # docker reads 0 as "unlimited": 0 must not silently remove a cap the caller asked for
        mem = re.match(r"\d+(\.\d+)?", str(self.memory))
        if not (self.cpus > 0 and self.pids > 0 and mem and float(mem.group()) > 0):
            raise ValueError(f"cpus, memory and pids must be > 0, got {self}")
