#!/usr/bin/env python3
"""Build raw_sample.jsonl + gold predictions for a chosen set of SWE-bench Pro instances.

The eval harness (swe_bench_pro_eval.py) needs raw_sample columns where
selected_test_files_to_run / fail_to_pass / pass_to_pass are STRING reprs of
lists (it calls eval() on them) and the f2p/p2p keys are LOWERCASE.
The source JSONL stores them as real lists under UPPERCASE keys, so convert.
"""
import argparse, json, sys

SRC = "/home/rob/swebench/SWE-bench_Pro-os/helper_code/sweap_eval_full_v2.jsonl"


def aslist(v):
    return v if isinstance(v, list) else json.loads(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", nargs="+", required=True)
    ap.add_argument("--raw_out", required=True)
    ap.add_argument("--gold_out", required=True)
    args = ap.parse_args()

    rows = {json.loads(l)["instance_id"]: json.loads(l) for l in open(SRC)}
    raw, gold = [], []
    for iid in args.instances:
        if iid not in rows:
            sys.exit(f"missing instance: {iid}")
        r = rows[iid]
        raw.append({
            "instance_id": iid,
            "repo": r["repo"],
            "base_commit": r["base_commit"],
            "before_repo_set_cmd": r["before_repo_set_cmd"],
            "selected_test_files_to_run": json.dumps(aslist(r["selected_test_files_to_run"])),
            "fail_to_pass": json.dumps(aslist(r["FAIL_TO_PASS"])),
            "pass_to_pass": json.dumps(aslist(r["PASS_TO_PASS"])),
        })
        gold.append({"instance_id": iid, "patch": r["patch"], "prefix": "gold"})

    with open(args.raw_out, "w") as f:
        for row in raw:
            f.write(json.dumps(row) + "\n")
    with open(args.gold_out, "w") as f:
        json.dump(gold, f, indent=2)
    print(f"wrote {len(raw)} raw rows -> {args.raw_out}")
    print(f"wrote {len(gold)} gold patches -> {args.gold_out}")


if __name__ == "__main__":
    main()
