import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    """Resource limits, enforced by gVisor/cgroups on the machine hosting the sandbox.

    cpus    CPU cores, fractions allowed (0.5).
    memory  Hard RAM cap in docker syntax ("512m", "4g"). Swap is disabled.
    pids    Max processes/threads (stops fork bombs).
    shm     /dev/shm size ("1g"). Default: docker's 64m, or 1g for gui.
    disk    Hard cap on the root filesystem: everything the sandbox writes outside /workspace and /tmp ("5g").
            A whole number + optional k/m/g/t (no decimals, no "gb"). It does NOT cover the /workspace volume of a
            named sandbox, and /tmp is RAM, counted in `memory`.
    workspace  Hard cap on the data volume of a named sandbox (/workspace; the home when gui), same syntax as `disk`.
            Default: none, and then a runaway process can fill the host's disk through it. A docker volume is a host
            directory, so this needs a host whose /var/lib/docker/volumes is on xfs mounted with `pquota` (the README
            says how): elsewhere create_sandbox raises instead of silently leaving the volume uncapped. Fixed when the
            volume is created. Named sandboxes only.
    """

    cpus: float = 2.0
    memory: str = "2g"
    pids: int = 512
    shm: str | None = None
    disk: str = "5g"
    workspace: str | None = None

    def __post_init__(self):
        # docker reads 0 as "unlimited": 0 must not silently remove a cap the caller asked for
        mem = re.match(r"\d+(\.\d+)?", str(self.memory))
        if not (self.cpus > 0 and self.pids > 0 and mem and float(mem.group()) > 0):
            raise ValueError(f"cpus, memory and pids must be > 0, got {self}")
        # runsc reads "0" as unlimited and rejects "1.5g"/"2gb" only when the container starts: fail here instead
        disk = re.fullmatch(r"(\d+)[kmgtKMGT]?", str(self.disk))
        if not (disk and int(disk[1]) > 0):
            raise ValueError(f"disk must be a whole number > 0 plus optional k/m/g/t (e.g. '5g'), got {self.disk!r}")
        # docker reads size=0 as "no quota": 0 must not silently remove a cap the caller asked for
        if self.workspace is not None:
            ws = re.fullmatch(r"(\d+)[kmgtKMGT]?", str(self.workspace))
            if not (ws and int(ws[1]) > 0):
                raise ValueError(f"workspace must be a whole number > 0 plus optional k/m/g/t (e.g. '10g'), got {self.workspace!r}")
