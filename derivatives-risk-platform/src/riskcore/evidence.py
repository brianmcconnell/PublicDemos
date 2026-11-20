"""Text evidence writer: every stage records what it did as markdown under evidence/.

The evaluator only reads text, so each notebook calls ``Evidence(stage)`` and appends tables,
checks and metrics; ``save()`` writes evidence/<stage>.md (workspace) and, if configured, a copy
to a UC volume. 99_collect_evidence stitches these into evidence/README.md.
"""
from __future__ import annotations

import datetime as _dt
import os

import pandas as pd


def df_to_md(df: pd.DataFrame, max_rows: int = 30, floatfmt: str = "{:,.6g}") -> str:
    if df is None or len(df) == 0:
        return "_(no rows)_\n"
    d = df.head(max_rows).copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].map(lambda v: "" if pd.isna(v) else floatfmt.format(v))
    cols = [str(c) for c in d.columns]
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for row in d.astype(str).itertuples(index=False):
        lines.append("| " + " | ".join(v.replace("|", "\\|") for v in row) + " |")
    more = f"\n_{len(df) - max_rows} more rows not shown (total {len(df)})_\n" if len(df) > max_rows else "\n"
    return "\n".join(lines) + more


class Evidence:
    def __init__(self, stage: str, root: str | None = None, run_id: str | None = None):
        self.stage = stage
        self.root = root or os.environ.get("RISK_EVIDENCE_DIR", "evidence")
        self.run_id = run_id
        self.parts: list[str] = []
        self.checks: list[dict] = []
        self.started = _dt.datetime.utcnow()

    def h(self, text: str):
        self.parts.append(f"\n### {text}\n")

    def text(self, text: str):
        self.parts.append(text.rstrip() + "\n")

    def table(self, title: str, df: pd.DataFrame, max_rows: int = 30):
        self.parts.append(f"\n**{title}**\n\n" + df_to_md(df, max_rows))

    def metric(self, name: str, value):
        self.parts.append(f"* {name}: `{value}`\n")

    def check(self, name: str, passed: bool, detail: str = ""):
        self.checks.append(dict(check=name, result="PASS" if passed else "FAIL", detail=detail))
        return passed

    @property
    def all_passed(self) -> bool:
        return all(c["result"] == "PASS" for c in self.checks)

    def render(self) -> str:
        end = _dt.datetime.utcnow()
        head = [f"## Stage: {self.stage}\n",
                f"* started (UTC): `{self.started:%Y-%m-%d %H:%M:%S}`",
                f"* finished (UTC): `{end:%Y-%m-%d %H:%M:%S}`  ({(end - self.started).total_seconds():.1f}s)"]
        if self.run_id:
            head.append(f"* job run: `{self.run_id}`")
        body = "\n".join(head) + "\n"
        if self.checks:
            body += "\n**Checks**\n\n" + df_to_md(pd.DataFrame(self.checks), max_rows=200)
        return body + "".join(self.parts)

    def save(self, extra_dir: str | None = None) -> str:
        md = self.render()
        for d in [self.root] + ([extra_dir] if extra_dir else []):
            try:
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, f"{self.stage}.md"), "w") as fh:
                    fh.write(md)
            except OSError as exc:              # evidence must never break the pipeline
                print(f"evidence write to {d} failed: {exc}")
        print(md[:4000])
        return md
