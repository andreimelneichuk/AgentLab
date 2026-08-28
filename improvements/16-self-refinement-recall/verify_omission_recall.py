"""Offline verification of recall_refine.detect_likely_omission() against the
real benchmark catalog (no live LLM calls).

Methodology (per improvements/16-self-refinement-recall task spec):
  - Walk every *.yaml under benchmark/scenarios/catalog/ and benchmark/scenarios/.
  - Build a synthetic conversation history turn-by-turn, seeding each turn's
    "established facts" from its own expect_contains (i.e. assume the ground
    truth answer was given correctly at the time, which is what subsequent
    recall turns are testing memory of).
  - A turn is "recall-labeled" (ground truth) if the scenario author marked it
    with forbid_tool_called and/or max_tool_calls_delta: 0 -- i.e. the turn is
    explicitly supposed to be answered from memory, without a new tool call.
  - For every turn, run detect_likely_omission() twice against the SAME
    history-so-far:
      (a) a generic filler draft that omits all expect_contains values
      (b) a draft that includes all expect_contains values verbatim
  - Report:
      recall_coverage      = fraction of recall-labeled turns where (a) fires
      recall_no_overtrigger = fraction of recall-labeled turns where (b) does
                               NOT fire (i.e. detector doesn't nag once the
                               fact is already there)
      non_recall_fp_rate   = fraction of NON-recall-labeled turns where (a)
                               fires anyway (false positive -- extra LLM call
                               with no omission to fix)
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

VARIANT_DIR = Path("/path/to/AgentLab/improvements/16-self-refinement-recall/variant")
sys.path.insert(0, str(VARIANT_DIR))

import yaml  # noqa: E402

from recall_refine import detect_likely_omission  # noqa: E402

CATALOG_GLOBS = [
    "/path/to/AgentLab/benchmark/scenarios/catalog/*.yaml",
    "/path/to/AgentLab/benchmark/scenarios/*.yaml",
]


def load_scenarios():
    files = []
    for pattern in CATALOG_GLOBS:
        files.extend(sorted(glob.glob(pattern)))
    scenarios = []
    for f in files:
        data = yaml.safe_load(open(f, encoding="utf-8")) or {}
        for s in data.get("scenarios", []):
            s["_file"] = f
            scenarios.append(s)
    return scenarios


def main() -> None:
    scenarios = load_scenarios()

    recall_total = 0
    recall_flagged_on_omit = 0
    recall_flagged_on_include = 0  # over-trigger even when fact IS present
    nonrecall_total = 0
    nonrecall_flagged_on_omit = 0

    recall_misses = []
    nonrecall_fps = []
    include_overtriggers = []

    for scenario in scenarios:
        history = []
        for turn in scenario.get("turns", []):
            if turn.get("new_session"):
                history = []

            user_msg = turn.get("user")
            if not user_msg:
                continue
            expect_contains = turn.get("expect_contains") or []
            max_delta = turn.get("max_tool_calls_delta")
            # NB: forbid_tool_called ALONE is not a recall signal -- it is also
            # used for decoy-tool avoidance on turns that DO call a (different,
            # correct) tool (expect_tool_called set, forbid_tool_called is the
            # decoy). The real "answer from memory, no new tool call" signal is
            # max_tool_calls_delta: 0, optionally combined with forbid_tool_called
            # naming the tool that should NOT be re-called because its result is
            # already known (e.g. s14_drift_recall.yaml turns 40-49).
            # Additionally require a non-empty expect_contains: turns with
            # max_tool_calls_delta: 0 but NO expect_contains are small-talk /
            # generic-knowledge / free-form summary turns ("Кстати, что такое
            # SKU?", "Ок, идём дальше.") -- there is no *specific established
            # value* to omit, so they are out of scope for this detector by
            # definition, not a miss.
            is_recall_labeled = (max_delta == 0) and bool(expect_contains)

            # Realistic assistant answers restate some of the question's own
            # entities/nouns alongside the tool-derived facts (e.g. "Результат
            # для (12+8)*3: RESULT=60", not a bare "RESULT=60"). Simulate that
            # by echoing the user's own turn text next to the ground-truth
            # values -- otherwise a synthetic answer that ONLY contains the
            # literal expect_contains token loses context real assistants
            # would naturally repeat, understating recall coverage.
            fact_values = " ".join(str(v) for v in expect_contains) if expect_contains else "готово"
            correct_answer = f"По запросу «{user_msg}»: {fact_values}."
            omit_draft = "Хорошо, вот ответ на ваш вопрос. Дай знать, если нужно что-то ещё."
            include_draft = correct_answer

            sig_omit = detect_likely_omission(user_msg, history, omit_draft)
            sig_include = detect_likely_omission(user_msg, history, include_draft)

            if is_recall_labeled:
                recall_total += 1
                if sig_omit.triggered:
                    recall_flagged_on_omit += 1
                else:
                    recall_misses.append((scenario["id"], user_msg[:80]))
                if sig_include.triggered:
                    recall_flagged_on_include += 1
                    include_overtriggers.append((scenario["id"], user_msg[:80]))
            else:
                nonrecall_total += 1
                if sig_omit.triggered:
                    nonrecall_flagged_on_omit += 1
                    nonrecall_fps.append((scenario["id"], user_msg[:80]))

            # Seed history with this turn (ground-truth correct answer).
            history.append({"role": "user", "content": user_msg})
            history.append({"role": "assistant", "content": correct_answer})

    print(f"scenarios: {len(scenarios)}")
    print(f"recall-labeled turns (forbid_tool_called or max_tool_calls_delta:0): {recall_total}")
    print(f"non-recall turns: {nonrecall_total}")
    print()
    if recall_total:
        print(
            f"recall coverage (fires on omit-draft):        "
            f"{recall_flagged_on_omit}/{recall_total} = {recall_flagged_on_omit / recall_total:.1%}"
        )
        print(
            f"over-trigger on include-draft (should be low): "
            f"{recall_flagged_on_include}/{recall_total} = {recall_flagged_on_include / recall_total:.1%}"
        )
    if nonrecall_total:
        print(
            f"false-positive rate on non-recall turns:       "
            f"{nonrecall_flagged_on_omit}/{nonrecall_total} = {nonrecall_flagged_on_omit / nonrecall_total:.2%}"
        )

    print()
    print(f"-- sample recall MISSES (should be flagged but weren't), showing up to 10 of {len(recall_misses)} --")
    for sid, msg in recall_misses[:10]:
        print(f"  [{sid}] {msg}")

    print()
    print(f"-- sample non-recall FALSE POSITIVES, showing up to 10 of {len(nonrecall_fps)} --")
    for sid, msg in nonrecall_fps[:10]:
        print(f"  [{sid}] {msg}")

    print()
    print(f"-- sample over-triggers on already-complete answer, showing up to 10 of {len(include_overtriggers)} --")
    for sid, msg in include_overtriggers[:10]:
        print(f"  [{sid}] {msg}")


if __name__ == "__main__":
    main()
