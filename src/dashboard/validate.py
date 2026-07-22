"""每日 Dashboard 导出产物的生产验收门。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SUMMARY_FIELDS = ("daily_summary", "campaign_summary", "product_summary")
LISTING_FIELDS = ("listing_type", "listing_status", "token_category", "markets")
ALLOWED_DIFF_TAGS = {"missing", "diff", "broad", "na"}


def validate_dashboard_payload(payload: dict[str, Any], expected_date: str) -> list[str]:
    issues: list[str] = []
    actual_date = payload.get("meta", {}).get("batch_date")
    if actual_date != expected_date:
        issues.append(f"batch_date_mismatch:{actual_date!r}!={expected_date!r}")

    digest = payload.get("daily_digest") or {}
    if digest.get("source") != "llm":
        issues.append(f"daily_digest_source_not_llm:{digest.get('source')!r}")
    for field in SUMMARY_FIELDS:
        if not isinstance(digest.get(field), str) or not digest[field].strip():
            issues.append(f"daily_digest_missing:{field}")

    listing_rows = payload.get("listing_all") or payload.get("listing") or []
    for index, row in enumerate(listing_rows):
        missing = [field for field in LISTING_FIELDS if field not in row]
        if missing:
            issues.append(f"listing_row_{index}_missing:{','.join(missing)}")

    canonical_by_group: dict[tuple[str, str], str] = {}
    for section in ("campaign_all", "product_all"):
        for index, row in enumerate(payload.get(section) or []):
            tag = row.get("diff_tag")
            if tag not in ALLOWED_DIFF_TAGS:
                issues.append(f"{section}_row_{index}_invalid_diff_tag:{tag!r}")
            if row.get("comparison_status") == "analyzed" and tag == "na":
                issues.append(f"{section}_row_{index}_analyzed_but_not_compared")
            canonical_by_group[(row.get("source"), row.get("group_id") or row.get("uid"))] = tag
    for index, row in enumerate((payload.get("search_index") or {}).get("rows") or []):
        tag = row.get("diff_tag")
        if tag not in ALLOWED_DIFF_TAGS:
            issues.append(f"search_row_{index}_invalid_diff_tag:{tag!r}")
        key = (row.get("source"), row.get("group_id") or row.get("uid"))
        if key in canonical_by_group and canonical_by_group[key] != tag:
            issues.append(f"search_row_{index}_diff_tag_mismatch:{tag!r}!={canonical_by_group[key]!r}")
    return issues


def main() -> None:
    parser = argparse.ArgumentParser(description="验收每日 Dashboard JSON 的关键分析输出")
    parser.add_argument("--input", required=True, help="dashboard.json 路径")
    parser.add_argument("--date", required=True, help="预期批次日期 YYYY-MM-DD")
    args = parser.parse_args()

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    issues = validate_dashboard_payload(payload, args.date)
    if issues:
        print(json.dumps({"valid": False, "issues": issues}, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps({
        "valid": True,
        "batch_date": args.date,
        "required_summaries": list(SUMMARY_FIELDS),
        "listing_rows_checked": len(payload.get("listing_all") or payload.get("listing") or []),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
