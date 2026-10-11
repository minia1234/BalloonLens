"""Blind A/B comparison for people to grade. No model judges the output.

prepare: takes pairs of translations (local vs subscription) for the same source lines, puts each pair in a random
         A/B order, and writes two files:
           sheet.csv  - what the grader sees: id, source, A, B
           key.json   - which system is A and which is B. Keep it away from the grader until grading ends.
score:   reads the grader's answers.csv with key.json and reports, per system, preference, naturalness and
         meaning-error rates. Thresholds from the design doc are printed as PROPOSED until the user approves them.

Usage:
  python tools/blind_eval.py prepare --pairs pairs.json --out-dir bench_out/blind/run1 --seed 20261010
  python tools/blind_eval.py score --key bench_out/blind/run1/key.json --answers bench_out/blind/run1/answers.csv

pairs.json: [{"id": "p01", "source": "待って！", "local": "잠깐!", "subscription": "기다려!"}, ...]
answers.csv columns: id,choice,a_naturalness,b_naturalness,a_meaning_error,b_meaning_error
  choice: A, B or tie. naturalness: 1-5. meaning_error: 1 if that side changes the meaning, else 0.
"""
import argparse
import csv
import json
import random
import statistics
from pathlib import Path

SYSTEMS = ("local", "subscription")


def prepare(args):
    pairs = json.loads(Path(args.pairs).read_text(encoding="utf-8"))
    rng = random.Random(args.seed)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    key = {}
    with open(out / "sheet.csv", "w", encoding="utf-8", newline="") as sheet:
        writer = csv.writer(sheet)
        writer.writerow(["id", "source", "A", "B"])
        for row in pairs:
            if not all(k in row for k in ("id", "source", *SYSTEMS)):
                raise SystemExit(f"pair {row.get('id', '?')} needs id, source, local and subscription")
            first, second = list(SYSTEMS)
            if rng.random() < 0.5:
                first, second = second, first
            key[row["id"]] = {"A": first, "B": second}
            writer.writerow([row["id"], row["source"], row[first], row[second]])
    (out / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out / "answers.csv", "w", encoding="utf-8", newline="") as answers:
        writer = csv.writer(answers)
        writer.writerow(["id", "choice", "a_naturalness", "b_naturalness", "a_meaning_error", "b_meaning_error"])
        for row in pairs:
            writer.writerow([row["id"], "", "", "", "", ""])
    print(f"{len(pairs)} pairs written to {out}")
    print("Give the grader sheet.csv and answers.csv only. key.json stays with the person who runs score.")


def score(args):
    key = json.loads(Path(args.key).read_text(encoding="utf-8"))
    with open(args.answers, encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["choice"].strip()]
    if not rows:
        raise SystemExit("no graded rows in answers.csv")
    wins = {s: 0 for s in SYSTEMS}
    ties = 0
    natural = {s: [] for s in SYSTEMS}
    meaning = {s: [] for s in SYSTEMS}
    for row in rows:
        mapping = key[row["id"]]
        choice = row["choice"].strip().upper()
        if choice == "TIE":
            ties += 1
        elif choice in ("A", "B"):
            wins[mapping[choice]] += 1
        else:
            raise SystemExit(f"bad choice for {row['id']}: {row['choice']}")
        for side in ("A", "B"):
            system = mapping[side]
            natural[system].append(int(row[f"{side.lower()}_naturalness"]))
            meaning[system].append(int(row[f"{side.lower()}_meaning_error"]))

    report = {"graded": len(rows), "ties": ties, "wins": wins, "systems": {}}
    for system in SYSTEMS:
        report["systems"][system] = {
            "naturalness_mean": round(statistics.fmean(natural[system]), 2),
            "meaning_error_rate": round(statistics.fmean(meaning[system]), 3),
        }
    local, sub = report["systems"]["local"], report["systems"]["subscription"]
    report["proposed_criteria"] = {
        "status": "PROPOSED - not approved; design doc section 14.6",
        "meaning_errors_at_least_30_percent_fewer": sub["meaning_error_rate"] <= 0.7 * local["meaning_error_rate"]
        if local["meaning_error_rate"] > 0 else None,
        "subscription_naturalness_at_least_4": sub["naturalness_mean"] >= 4.0,
        "naturalness_gain_at_least_0_5": round(sub["naturalness_mean"] - local["naturalness_mean"], 2) >= 0.5,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--pairs", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--seed", type=int, default=1)
    p.set_defaults(run=prepare)
    s = sub.add_parser("score")
    s.add_argument("--key", required=True)
    s.add_argument("--answers", required=True)
    s.set_defaults(run=score)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
