import json

from src.analysis.listing import (
    TOKEN_CATEGORIES,
    derive_listing_facts,
    validate_listing_classification,
)


def test_listing_classifier_accepts_only_controlled_categories():
    result = validate_listing_classification(
        json.dumps({"items": [
            {"i": 1, "category": "Meme", "confidence": 0.8},
            {"i": 2, "category": "made-up-sector", "confidence": 2},
        ]}),
        expected_indices={1, 2, 3},
    )
    assert result.valid
    assert result.categories == {1: "Meme", 2: "Other", 3: "Other"}
    assert result.confidences[2] == 1.0
    assert set(result.categories.values()) <= set(TOKEN_CATEGORIES)


def test_listing_facts_are_deterministic_and_not_llm_judgments():
    facts = derive_listing_facts(
        "Exchange Will List ABC/USDT Perpetual Contract",
        "Trading begins 2026-07-22 08:00 UTC.",
        "listing",
    )
    assert facts == {
        "token_symbol": "ABC",
        "trading_pair": "ABC/USDT",
        "listing_type": "Perpetual",
        "listing_status": "New Listing",
        "launch_time": "2026-07-22 08:00 UTC",
    }


def test_delisting_status_is_derived_from_fact_category():
    assert derive_listing_facts("Delist XYZ/USDT", "", "delisting")["listing_status"] == "Delisted"


def test_known_spot_section_resolves_generic_listing_title():
    facts = derive_listing_facts(
        "Initial listing: ABC now available", "", "listing",
        source="Weex", raw_category="18540509241753",
    )
    assert facts["listing_type"] == "Spot"


def test_explicit_perpetual_evidence_overrides_spot_section_default():
    facts = derive_listing_facts(
        "ABCUSDT Perpetual Contract Is Now Live", "", "listing",
        source="Bitunix", raw_category="13762037166105",
    )
    assert facts["listing_type"] == "Perpetual"


def test_combined_spot_and_perpetual_announcement_is_not_unknown():
    facts = derive_listing_facts(
        "ABC listing", "Spot Trading opens first. ABCUSDT perpetual futures follows.", "listing",
        source="Bitunix", raw_category="13762037166105",
    )
    assert facts["listing_type"] == "Spot & Perpetual"
