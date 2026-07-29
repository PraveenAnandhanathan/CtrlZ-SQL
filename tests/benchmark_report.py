"""Collect benchmark measurements and publish them.

Spec §10.6 asks for benchmarks that "run in CI **and publish numbers**". They
ran, and asserted a budget, but every measurement was thrown away the moment
the assertion passed -- so the only thing CI could tell you was that nothing had
regressed by an order of magnitude. That is a tripwire, not a benchmark.

An assertion answers "is it still acceptable". A published number answers "what
is it, and which way is it moving", which is the question you have when someone
asks whether the gateway is fast enough for their workload.

The results go to the terminal always, and to the GitHub Actions job summary
when running there, so each build leaves a readable record without anyone
adding a dashboard.
"""

from __future__ import annotations

import os
import statistics
from dataclasses import dataclass, field


@dataclass
class Measurement:
    name: str
    #: What the number describes, e.g. "one statement, analysed".
    unit: str
    median_ms: float
    p99_ms: float
    budget_ms: float | None = None
    note: str = ""

    @property
    def over_budget(self) -> bool:
        return bool(self.budget_ms) and self.p99_ms > self.budget_ms

    @property
    def headroom(self) -> str:
        """How far inside the budget the p99 sits.

        Rounded to whole multiples this read "1x" for a p99 of 1.23 ms against
        a 1.00 ms budget -- a number over budget, displayed as if it were not.
        A summary that rounds a miss into a pass is worse than no summary.
        """
        if not self.budget_ms or not self.p99_ms:
            return "-"
        ratio = self.budget_ms / self.p99_ms
        if ratio < 1:
            return f"OVER ({ratio:.2f}x)"
        return f"{ratio:.1f}x" if ratio < 10 else f"{ratio:.0f}x"


@dataclass
class Report:
    measurements: list[Measurement] = field(default_factory=list)

    def record(self, name: str, unit: str, timings: list[float],
               budget_ms: float | None = None, note: str = "") -> Measurement:
        ordered = sorted(timings)
        index = min(len(ordered) - 1, int(len(ordered) * 0.99))
        measurement = Measurement(
            name=name,
            unit=unit,
            median_ms=statistics.median(ordered),
            p99_ms=ordered[index],
            budget_ms=budget_ms,
            note=note,
        )
        self.measurements.append(measurement)
        return measurement

    # -- output ------------------------------------------------------------

    def as_table(self) -> list[str]:
        if not self.measurements:
            return []
        width = max(len(m.name) for m in self.measurements)
        lines = [
            f"{'measurement'.ljust(width)}   median      p99   budget  headroom",
            f"{'-' * width}   ------   ------   ------  --------",
        ]
        for m in self.measurements:
            budget = f"{m.budget_ms:.2f}" if m.budget_ms else "     -"
            lines.append(
                f"{m.name.ljust(width)}   {m.median_ms:6.4f}   {m.p99_ms:6.4f}   "
                f"{budget:>6}  {m.headroom:>13}"
                + (f"   {m.note}" if m.note else "")
            )
        missed = [m for m in self.measurements if m.over_budget]
        if missed:
            lines += [
                "",
                "over budget: " + ", ".join(m.name for m in missed),
                "  Recorded rather than hidden. The tests still pass because the "
                "assertion",
                "  is an order-of-magnitude tripwire, not the budget itself -- "
                "see NFR-2.",
            ]
        return lines

    def as_markdown(self) -> str:
        rows = [
            "### ctrlz benchmarks",
            "",
            "All times in milliseconds, measured on the CI runner for this build.",
            "",
            "| measurement | median | p99 | budget | headroom |",
            "|---|---:|---:|---:|---:|",
        ]
        for m in self.measurements:
            budget = f"{m.budget_ms:.2f}" if m.budget_ms else "—"
            rows.append(
                f"| {m.name} — *{m.unit}* | {m.median_ms:.4f} | {m.p99_ms:.4f} "
                f"| {budget} | {m.headroom}{(' — ' + m.note) if m.note else ''} |"
            )
        rows += ["", "Budgets come from NFR-1 and NFR-2 in `spec/spec.md`."]
        missed = [m for m in self.measurements if m.over_budget]
        if missed:
            rows += [
                "",
                "**Over budget:** " + ", ".join(f"`{m.name}`" for m in missed)
                + ". Recorded rather than hidden.",
            ]
        return "\n".join(rows)

    def publish(self, terminalreporter=None) -> None:
        """Write the numbers wherever this run can leave a record."""
        if not self.measurements:
            return
        if terminalreporter is not None:
            terminalreporter.write_sep("-", "benchmarks")
            for line in self.as_table():
                terminalreporter.write_line(line)

        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            try:
                with open(summary, "a", encoding="utf-8") as handle:
                    handle.write(self.as_markdown() + "\n")
            except OSError:
                # Publishing is a convenience. Failing to write a summary file
                # must never turn a green build red.
                pass


#: One report per run, shared by the benchmark tests via the `benchmark`
#: fixture in conftest.
REPORT = Report()
