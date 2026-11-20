"""Counterparty exposure aggregation (EE / PFE) and book value aggregation.

For every counterparty, time step and path:
  E = sum_{netting sets}  max( sum_{trades in set} V , 0 )  +  sum_{NA trades} max( V , 0 )
EE = mean over paths, PFE = 95th percentile over paths (sorted path distribution).
Book values are summed per (book, step, path) and returned as partial sums so they can be
added across Spark groups (a book spans many counterparties).

Distributed flow (load balanced, see notebook 13):
  1. ``assign_bins``: trades are grouped by netting key (netting set, or 'NA' per counterparty);
     large keys are split into chunks and chunks are packed into ~equal-size bins.
  2. ``run_bucket`` (one Spark group = one bin) prices its trades and emits PARTIAL path arrays:
     NS  = sum of V per netting set chunk (linear, so chunks can be added later)
     NA  = sum of max(V,0) per counterparty for trades without a netting agreement
     BOOK= sum of V per book; plus TRADE stats and SAMPLE paths.
  3. ``finalize_counterparty``: add partials per key, floor netting sets at zero, sum to the
     counterparty and take EE / PFE95 per time step.

Exposures are uncollateralised; collateral agreements are carried as reference data.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PCTS = (5, 25, 50, 75, 95)

OUTPUT_COLUMNS = ["record_type", "counterparty_id", "netting_set_id", "book_id", "trade_id", "product",
                  "time_step", "path_id", "n_trades", "ee", "pfe_95", "p5", "p25", "p50", "p75", "p95",
                  "mean_pv", "t0_pv", "pv", "pv_paths"]


def dist_stats(X: np.ndarray) -> dict:
    """X (steps, paths) -> per-step mean and percentiles."""
    q = np.percentile(X, PCTS, axis=1)
    return dict(mean=X.mean(axis=1), **{f"p{p}": q[i] for i, p in enumerate(PCTS)})


class Accumulator:
    def __init__(self, n_steps: int, n_paths: int):
        self.S, self.P = n_steps, n_paths
        self.ns_sum: dict = {}          # (cpty, ns) -> (S,P) sum of V
        self.na_pos: dict = {}          # cpty -> (S,P) sum of max(V,0) for NA trades
        self.book: dict = {}            # book -> (S,P) sum of V
        self.n_ns: dict = {}
        self.n_na: dict = {}
        self.n_book: dict = {}
        self.trade_rows: list = []
        self.sample_rows: list = []

    def _acc(self, store, counts, key, X, n):
        if key in store:
            store[key] += X
        else:
            store[key] = X.copy()
        counts[key] = counts.get(key, 0) + n

    def add(self, trades: pd.DataFrame, V: np.ndarray, sample_mask=None, sample_paths: int = 20):
        cp = trades["counterparty_id"].to_numpy()
        ns = trades["netting_set_id"].fillna("NA").to_numpy()
        bk = trades["book_id"].to_numpy()
        is_na = ns == "NA"
        for c in np.unique(cp[is_na]):
            m = is_na & (cp == c)
            self._acc(self.na_pos, self.n_na, c, np.maximum(V[m], 0.0).sum(axis=0), int(m.sum()))
        for key in set(zip(cp[~is_na], ns[~is_na])):
            m = (~is_na) & (cp == key[0]) & (ns == key[1])
            self._acc(self.ns_sum, self.n_ns, key, V[m].sum(axis=0), int(m.sum()))
        for b in np.unique(bk):
            m = bk == b
            self._acc(self.book, self.n_book, b, V[m].sum(axis=0), int(m.sum()))
        # trade-level distribution statistics
        mean = V.mean(axis=2)
        q = np.percentile(V, [5, 95], axis=2)
        t0 = V[:, 0, 0]
        ids, prods = trades["trade_id"].to_numpy(), trades["product"].to_numpy()
        for i in range(len(trades)):
            self.trade_rows.append(pd.DataFrame({
                "record_type": "TRADE", "trade_id": ids[i], "product": prods[i], "counterparty_id": cp[i],
                "netting_set_id": ns[i], "book_id": bk[i], "time_step": np.arange(self.S),
                "mean_pv": mean[i], "p5": q[0, i], "p95": q[1, i], "t0_pv": t0[i]}))
        if sample_mask is not None and np.any(sample_mask):
            k = min(sample_paths, self.P)
            for i in np.flatnonzero(sample_mask):
                self.sample_rows.append(pd.DataFrame({
                    "record_type": "SAMPLE", "trade_id": ids[i], "product": prods[i],
                    "counterparty_id": cp[i], "netting_set_id": ns[i], "book_id": bk[i],
                    "time_step": np.repeat(np.arange(self.S), k), "path_id": np.tile(np.arange(k), self.S),
                    "pv": V[i, :, :k].ravel()}))

    def exposure_rows(self) -> pd.DataFrame:
        rows = []
        cps = set(self.na_pos) | {c for c, _ in self.ns_sum}
        for c in sorted(cps):
            total = np.zeros((self.S, self.P))
            parts = []
            if c in self.na_pos:
                parts.append(("NA", self.na_pos[c], self.n_na[c]))
            for (cc, ns), X in self.ns_sum.items():
                if cc == c:
                    parts.append((ns, np.maximum(X, 0.0), self.n_ns[(cc, ns)]))
            for ns, E, n in parts:
                total += E
                rows.append(_exp_frame(c, ns, E, n))
            rows.append(_exp_frame(c, "ALL", total, sum(p[2] for p in parts)))
        return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=OUTPUT_COLUMNS)

    def book_rows(self) -> pd.DataFrame:
        rows = []
        for b, X in self.book.items():
            rows.append(pd.DataFrame({"record_type": "BOOK", "book_id": b, "time_step": np.arange(self.S),
                                      "n_trades": self.n_book[b], "pv_paths": [list(map(float, r)) for r in X]}))
        return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=OUTPUT_COLUMNS)

    def part_rows(self) -> pd.DataFrame:
        rows = []
        for (c, ns), X in self.ns_sum.items():
            rows.append(pd.DataFrame({"record_type": "PART_NS", "counterparty_id": c, "netting_set_id": ns,
                                      "time_step": np.arange(self.S), "n_trades": self.n_ns[(c, ns)],
                                      "pv_paths": [list(map(float, r)) for r in X]}))
        for c, X in self.na_pos.items():
            rows.append(pd.DataFrame({"record_type": "PART_NA", "counterparty_id": c, "netting_set_id": "NA",
                                      "time_step": np.arange(self.S), "n_trades": self.n_na[c],
                                      "pv_paths": [list(map(float, r)) for r in X]}))
        return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=OUTPUT_COLUMNS)

    def output(self, partial: bool = True) -> pd.DataFrame:
        head = self.part_rows() if partial else self.exposure_rows()
        frames = [head, self.book_rows()] + self.trade_rows + self.sample_rows
        out = pd.concat([f for f in frames if len(f)], ignore_index=True)
        for c in OUTPUT_COLUMNS:
            if c not in out:
                out[c] = None
        return out[OUTPUT_COLUMNS]


def _exp_frame(cpty, ns, E, n) -> pd.DataFrame:
    st = dist_stats(E)
    return pd.DataFrame({"record_type": "EXPOSURE", "counterparty_id": cpty, "netting_set_id": ns,
                         "time_step": np.arange(E.shape[0]), "n_trades": n, "ee": st["mean"],
                         "pfe_95": st["p95"], "p5": st["p5"], "p25": st["p25"], "p50": st["p50"],
                         "p75": st["p75"], "p95": st["p95"]})


def assign_bins(key_counts: pd.DataFrame, target: int = 1500, n_bins: int = 160) -> pd.DataFrame:
    """key_counts: columns key, n. Returns key, n_chunks, chunk, bin (greedy longest-first packing)."""
    items = []
    for key, n in zip(key_counts["key"], key_counts["n"]):
        k = max(1, int(np.ceil(n / target)))
        for c in range(k):
            items.append((key, k, c, n / k))
    items.sort(key=lambda it: -it[3])
    load = np.zeros(n_bins)
    out = []
    for key, k, c, w in items:
        b = int(np.argmin(load))
        load[b] += w
        out.append(dict(key=key, n_chunks=k, chunk=c, bin=b))
    return pd.DataFrame(out)


def finalize_counterparty(pdf: pd.DataFrame) -> pd.DataFrame:
    """One group = one counterparty's PART_NS / PART_NA rows -> EXPOSURE rows (EE, PFE95)."""
    c = pdf["counterparty_id"].iloc[0]
    S = int(pdf["time_step"].max()) + 1
    parts = {}
    counts = {}
    for (rt, ns), g in pdf.groupby(["record_type", "netting_set_id"]):
        X = None
        for step, gg in g.groupby("time_step"):
            arr = np.sum(np.stack([np.asarray(a, float) for a in gg["pv_paths"]]), axis=0)
            if X is None:
                X = np.zeros((S, arr.size))
            X[int(step)] += arr
        parts[ns] = np.maximum(X, 0.0) if rt == "PART_NS" else X
        counts[ns] = int(g.groupby("time_step")["n_trades"].sum().iloc[0])
    total = None
    frames = []
    for ns, E in parts.items():
        frames.append(_exp_frame(c, ns, E, counts[ns]))
        total = E.copy() if total is None else total + E
    frames.append(_exp_frame(c, "ALL", total, sum(counts.values())))
    out = pd.concat(frames, ignore_index=True)
    return out[["counterparty_id", "netting_set_id", "time_step", "n_trades", "ee", "pfe_95",
                "p5", "p25", "p50", "p75", "p95"]]


def finalize_book(pdf: pd.DataFrame) -> pd.DataFrame:
    """One group = one book's BOOK partial rows -> VaR statistics per time step."""
    from .var import book_var
    b = pdf["book_id"].iloc[0]
    S = int(pdf["time_step"].max()) + 1
    X = None
    for step, g in pdf.groupby("time_step"):
        arr = np.sum(np.stack([np.asarray(a, float) for a in g["pv_paths"]]), axis=0)
        if X is None:
            X = np.zeros((S, arr.size))
        X[int(step)] += arr
    out = book_var(b, X)
    out["n_trades"] = int(pdf.groupby("time_step")["n_trades"].sum().iloc[0])
    return out


def run_bucket(pdf: pd.DataFrame, cube, chunk: int = 100, sample_paths: int = 20) -> pd.DataFrame:
    """Price one Spark group (bin) of trades and emit partial aggregates."""
    from .pricers_vec import price_trades
    acc = Accumulator(cube.n_steps, cube.n_paths)
    pdf = pdf.reset_index(drop=True)
    for s in range(0, len(pdf), chunk):
        part = pdf.iloc[s:s + chunk].reset_index(drop=True)
        V = price_trades(part, cube)
        V = np.nan_to_num(V, nan=0.0, posinf=0.0, neginf=0.0)
        smask = part["is_sample"].to_numpy(bool) if "is_sample" in part else None
        acc.add(part, V, smask, sample_paths)
    return acc.output()
