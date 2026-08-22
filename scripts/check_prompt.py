#!/usr/bin/env python3
"""Fail if AGENTS.md and the tool contracts have drifted apart.

The prompt tells the model which values are legal. server.py decides which
values are actually accepted. If those two disagree, the model emits a value
the tool rejects, mid-demo. Run this after any edit to either file.
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp-server"))
import server  # noqa: E402

PROMPT = os.path.join(ROOT, "AGENTS.md")
MEMORY_TYPES = ("barrier", "contact_preference", "treatment_history",
                "communication_preference", "other")
REAL_TOOLS = {"get_patient_meds", "remember_patient_fact", "log_outcome",
              "schedule_followup"}

G, R, D, X = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def main() -> int:
    text = open(PROMPT).read()
    fails = []

    for label, vals in (("outcome", server.OUTCOMES),
                        ("non_adherence_reason", server.NON_ADHERENCE_REASONS),
                        ("escalation_red_flag_type", server.RED_FLAG_TYPES),
                        ("escalation_severity", server.SEVERITIES),
                        ("memory_type", MEMORY_TYPES)):
        missing = [v for v in vals if v not in text]
        if missing:
            fails.append(f"{label}: not documented in the prompt -> {missing}")
        else:
            print(f"  {G}OK{X} {label:26} {D}all {len(vals)} values documented{X}")

    for t in sorted(REAL_TOOLS):
        if t not in text:
            fails.append(f"tool {t} exists but the prompt never names it")

    # anything that looks like a tool call in the prompt must be a real tool
    for cand in set(re.findall(r"`([a-z][a-z_]{5,})\(", text)):
        if cand not in REAL_TOOLS:
            fails.append(f"prompt calls `{cand}()`, which is not a real tool")

    words = len(text.split())
    print(f"  {G}OK{X} {'all 4 tools named':30}")
    print(f"  {D}prompt length: {words} words{X}")
    if words > 1600:
        print(f"  {R}WARN{X} long for a reasoning model - the middle loses "
              f"attention")

    if fails:
        print(f"\n{R}DRIFT{X}")
        for f in fails:
            print(f"  - {f}")
        return 1
    print(f"\n{G}PROMPT AND TOOLS AGREE{X}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
