"""Slurm resource specification — §9."""
from __future__ import annotations

from dataclasses import dataclass, field, fields, replace


@dataclass
class SlurmResources:
    partition: str | None = None
    nodes: int | None = None
    ntasks: int | None = None
    cpus_per_task: int | None = None
    gres: str | None = None  # raw string for --gres, e.g. "gpu:2" or "gpu:a100:4"
    gpus: int | str | None = None  # 1  or  "a100:2"
    mem: str | None = None
    mem_per_cpu: str | None = None
    time: str | None = None
    constraint: str | None = None
    nodelist: str | None = None
    exclude: str | None = None
    array: str | None = None  # set automatically by .map()
    job_name: str | None = None
    extra_directives: list[str] = field(default_factory=list)
    # NOTE: no account/qos — add via extra_directives if ever needed.

    def merge(self, other: "SlurmResources | None") -> "SlurmResources":
        """Layer ``other`` on top of ``self`` (other wins where set)."""
        if other is None:
            return replace(self)
        merged = replace(self)
        for f in fields(self):
            if f.name == "extra_directives":
                continue
            val = getattr(other, f.name)
            if val is not None:
                setattr(merged, f.name, val)
        merged.extra_directives = [*self.extra_directives, *other.extra_directives]
        return merged

    def to_sbatch_lines(self) -> list[str]:
        lines: list[str] = []

        def add(flag: str, value):
            lines.append(f"#SBATCH --{flag}={value}")

        if self.job_name is not None:
            add("job-name", self.job_name)
        if self.partition is not None:
            add("partition", self.partition)
        if self.nodes is not None:
            add("nodes", self.nodes)
        if self.ntasks is not None:
            add("ntasks", self.ntasks)
        if self.cpus_per_task is not None:
            add("cpus-per-task", self.cpus_per_task)
        if self.gpus is not None:
            add("gres", f"gpu:{self.gpus}")
        if self.gres is not None:
            add("gres", self.gres)
        if self.mem is not None:
            add("mem", self.mem)
        if self.mem_per_cpu is not None:
            add("mem-per-cpu", self.mem_per_cpu)
        if self.time is not None:
            add("time", self.time)
        if self.constraint is not None:
            add("constraint", self.constraint)
        if self.nodelist is not None:
            add("nodelist", self.nodelist)
        if self.exclude is not None:
            add("exclude", self.exclude)
        if self.array is not None:
            add("array", self.array)
        lines.extend(self.extra_directives)
        return lines
