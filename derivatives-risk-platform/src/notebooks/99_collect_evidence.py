# Databricks notebook source
"""99_collect_evidence: stitch individual stage evidence files into evidence/README.md.

Also copies the evidence folder to the UC volume for external access.
"""
import sys, os, datetime as dt, glob
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("__file__")), ".."))

from riskcore.config import cfg_from_widgets

cfg = cfg_from_widgets(dbutils)
EVIDENCE_DIR = "evidence"

# COMMAND ----------
# Collect all .md files in evidence/
files = sorted(glob.glob(os.path.join(EVIDENCE_DIR, "*.md")))
if not files:
    print("No evidence files found")
    dbutils.notebook.exit("NO_EVIDENCE")

print(f"Found {len(files)} evidence files:")
for f in files:
    print(f"  {f}")

# COMMAND ----------
# Stitch into README.md
parts = [f"# Derivatives Risk Platform — Execution Evidence\n\n",
         f"Generated: {dt.datetime.utcnow():%Y-%m-%d %H:%M:%S} UTC\n\n",
         f"---\n\n"]
for f in files:
    if os.path.basename(f) == "README.md":
        continue
    with open(f) as fh:
        parts.append(fh.read())
    parts.append("\n---\n\n")

readme = "".join(parts)
with open(os.path.join(EVIDENCE_DIR, "README.md"), "w") as fh:
    fh.write(readme)
print(f"Wrote {len(readme)} chars to evidence/README.md")

# COMMAND ----------
# Copy to UC volume
vol_path = cfg.vol("evidence")
try:
    os.makedirs(vol_path, exist_ok=True)
    for f in files + [os.path.join(EVIDENCE_DIR, "README.md")]:
        dst = os.path.join(vol_path, os.path.basename(f))
        with open(f) as src, open(dst, "w") as tgt:
            tgt.write(src.read())
    print(f"Copied evidence to {vol_path}")
except Exception as exc:
    print(f"WARN: copy to volume failed: {exc}")

print("Evidence collection complete")
