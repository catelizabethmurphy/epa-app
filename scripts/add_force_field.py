"""Add a `force` field to each timeline item in static/data/timelines/*.json.

Derives an initial value from existing fields (typeLabel, signalType, withdrawn,
pressId) so editors only have to override the wrong ones.

Force values:
  final      — published final rule, designation in effect, court order
  proposed   — NPRM, ANPRM, request for comment, proposed legislation
  announced  — press release, statement, policy commitment, summit
  withdrawn  — formally pulled back
  court      — litigation filing / ruling
  (empty)    — fall through; no force inferred

Run:  python3 scripts/add_force_field.py
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
TIMELINES = ROOT / "static" / "data" / "timelines"


def derive_force(item: dict) -> str:
    tl = (item.get("typeLabel") or "").lower()
    sig = (item.get("signalType") or "").lower()

    if item.get("withdrawn"):
        return "withdrawn"
    if "proposed" in tl or "request for comment" in tl or "advanced notice" in tl:
        return "proposed"
    if "final" in tl or tl == "rule" or "designation" in tl:
        return "final"
    if sig == "litigation" or "court" in tl or "ruling" in tl:
        # Court orders/filings don't fit the proposed/final/announced axis.
        # Leave force empty so the chip shows just the direction word.
        return ""
    if (
        sig == "rhetoric"
        or "statement" in tl
        or "press" in tl
        or "commitment" in tl
        or "meeting" in tl
        or "summit" in tl
        or item.get("pressId")
    ):
        return "announced"
    return ""


def main() -> None:
    for path in sorted(TIMELINES.glob("*.json")):
        data = json.loads(path.read_text())
        timeline = data.get("timeline", [])
        changed = 0
        for item in timeline:
            if "force" in item:
                continue  # already set; don't overwrite manual edits
            item["force"] = derive_force(item)
            changed += 1
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print(f"{path.name}: added force to {changed}/{len(timeline)} items")


if __name__ == "__main__":
    main()
