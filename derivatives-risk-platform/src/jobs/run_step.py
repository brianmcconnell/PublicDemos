"""run_step: run one of the src/notebooks/*.py pipeline steps as a Python script task.

The step files are Databricks-source .py FILES (not notebooks), so a job notebook_task
cannot run them. This runner executes a step's source in-process with:
  * dbutils.widgets backed by the --param key=value arguments (missing keys fall back to
    the defaults the step passes to dbutils.widgets.text / riskcore.config.widget),
  * the real dbutils for secrets / fs,
  * notebook magics (%pip ...) stripped: dependencies come from the job environment,
  * dbutils.notebook.exit(...) treated as a normal successful end of the step,
  * display(df) printing the first rows.

Usage (job task parameters):
  --step 02_bootstrap_curves.py --param catalog=<cat> --param schema=market [--param k=v ...]
"""
import argparse
import os
import sys
import time

try:
    HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    HERE = os.getcwd()
NOTEBOOKS = os.path.abspath(os.path.join(HERE, "..", "notebooks"))
SRC = os.path.abspath(os.path.join(HERE, ".."))


class _StepExit(Exception):
    pass


class _Widgets:
    def __init__(self, values):
        self._v = dict(values)

    def text(self, name, default="", label=None):
        self._v.setdefault(name, "" if default is None else str(default))

    def dropdown(self, name, default="", choices=None, label=None):
        self.text(name, default)

    combobox = dropdown

    def get(self, name):
        if name not in self._v:
            raise ValueError(f"widget {name!r} not defined")
        return self._v[name]

    getArgument = get

    def getAll(self):
        return dict(self._v)

    def remove(self, name):
        self._v.pop(name, None)

    def removeAll(self):
        pass


class _Notebook:
    def exit(self, value=None):
        raise _StepExit(value)


class _DbUtils:
    """dbutils shim: widgets / notebook are local, everything else goes to the real dbutils."""

    def __init__(self, real, params):
        self._real = real
        self.widgets = _Widgets(params)
        self.notebook = _Notebook()

    def __getattr__(self, item):
        if self._real is None:
            raise AttributeError(f"dbutils.{item} is not available")
        return getattr(self._real, item)


def _display(obj, *a, **k):
    try:
        obj.show(20, truncate=False)
    except Exception:
        print(obj)


def _clean_source(text):
    out = []
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith("%") or s.startswith("# MAGIC"):
            out.append("# [run_step] stripped: " + s[:80])
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", required=True, help="file name under src/notebooks")
    ap.add_argument("--param", action="append", default=[], help="key=value widget value")
    args, _ = ap.parse_known_args()

    params = {}
    for kv in args.param:
        k, _, v = kv.partition("=")
        params[k.strip()] = v.strip()

    step_path = args.step if os.path.isabs(args.step) else os.path.join(NOTEBOOKS, args.step)
    with open(step_path) as fh:
        source = _clean_source(fh.read())

    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()
    try:
        from databricks.sdk.runtime import dbutils as real_dbutils
    except Exception:
        real_dbutils = None

    # steps resolve src/ via os.path.abspath("__file__") relative to the working directory
    os.chdir(os.path.dirname(step_path))
    if SRC not in sys.path:
        sys.path.insert(0, SRC)

    g = {
        "__name__": "__main__",
        "spark": spark,
        "dbutils": _DbUtils(real_dbutils, params),
        "display": _display,
        "displayHTML": print,
    }
    shown = {k: ("***" if "secret" in k else v) for k, v in params.items()}
    print(f"[run_step] {os.path.basename(step_path)} params={shown}")
    t0 = time.time()
    try:
        exec(compile(source, step_path, "exec"), g)
    except _StepExit as e:
        print(f"[run_step] step exited early: {e}")
    print(f"[run_step] {os.path.basename(step_path)} finished in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
