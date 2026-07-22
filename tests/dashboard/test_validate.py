from src.dashboard.validate import validate_dashboard_payload


def _payload():
    return {
        "meta": {"batch_date": "2026-07-21"},
        "daily_digest": {
            "source": "llm",
            "daily_summary": "总览摘要。",
            "campaign_summary": "活动摘要。",
            "product_summary": "产品摘要。",
        },
        "listing_all": [{
            "listing_type": "Spot",
            "listing_status": "New Listing",
            "token_category": "AI",
            "markets": ["EN"],
        }],
    }


def test_valid_dashboard_payload():
    assert validate_dashboard_payload(_payload(), "2026-07-21") == []


def test_rejects_missing_summary_and_listing_fields():
    payload = _payload()
    payload["daily_digest"]["source"] = "fallback"
    payload["daily_digest"]["campaign_summary"] = ""
    payload["listing_all"][0].pop("markets")
    issues = validate_dashboard_payload(payload, "2026-07-22")
    assert "batch_date_mismatch:'2026-07-21'!='2026-07-22'" in issues
    assert "daily_digest_source_not_llm:'fallback'" in issues
    assert "daily_digest_missing:campaign_summary" in issues
    assert "listing_row_0_missing:markets" in issues


def test_rejects_legacy_or_cross_tab_mismatched_diff_tags():
    payload = _payload()
    payload["campaign_all"] = [{"uid": "u1", "group_id": "g1", "source": "Bitunix", "diff_tag": "same"}]
    payload["search_index"] = {"rows": [
        {"uid": "u1", "group_id": "g1", "source": "Bitunix", "diff_tag": "diff"},
    ]}
    issues = validate_dashboard_payload(payload, "2026-07-21")
    assert "campaign_all_row_0_invalid_diff_tag:'same'" in issues
    assert "search_row_0_diff_tag_mismatch:'diff'!='same'" in issues
