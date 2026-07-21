from src.analysis.zmx_catalog import ZmxCatalogEntry, select_relevant_catalog


def _entry(uid: str, mechanism: str, title: str) -> ZmxCatalogEntry:
    return ZmxCatalogEntry(
        uid=uid,
        title=title,
        mechanism_type=mechanism,
        key_mechanics=None,
        reward_range=None,
        target_users=None,
        start_date=None,
        end_date=None,
        post_time="2026-07-01T00:00:00Z",
    )


def test_select_relevant_catalog_prefers_batch_terms_and_caps_output():
    rows = [{
        "title": "New copy trading stop loss feature",
        "content": "Copy traders can configure take profit and stop loss.",
    }]
    entries = [
        _entry("z1", "deposit_reward", "Deposit bonus campaign"),
        _entry("z2", "copy_trading_recruit", "Copy trading stop loss"),
        _entry("z3", "grid_trading_contest", "Futures competition"),
    ]

    selected = select_relevant_catalog(rows, entries, max_entries=2)

    assert len(selected) == 2
    assert selected[0].uid == "z2"


def test_select_relevant_catalog_keeps_original_order_when_under_cap():
    rows = [{"title": "anything", "content": ""}]
    entries = [_entry("z1", "A", "One"), _entry("z2", "B", "Two")]
    assert select_relevant_catalog(rows, entries, max_entries=8) == entries
