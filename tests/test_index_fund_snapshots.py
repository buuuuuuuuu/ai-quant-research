from ai_quant_assistant.api.app import create_app as _create_app  # noqa: F401
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.index_fund_snapshots import IndexFundSnapshotStore
from ai_quant_assistant.storage.layout import DataLayout


def test_snapshots_are_deduplicated_and_revisions_remain_readable(tmp_path):
    db = LocalDatabase(tmp_path / "db.duckdb", DataLayout.from_root(tmp_path / "data"))
    store = IndexFundSnapshotStore(db)
    first = store.save("000300", {"group": ["510300"]}, {"values": [1]}, {"points": [1]})
    assert store.save("000300", {"group": ["510300"]}, {"values": [1]}, {"points": [1]}) == first
    second = store.save("000300", {"group": ["510300"]}, {"values": [2]}, {"points": [2]})
    assert second != first
    assert store.get(first)["result"] == {"points": [1]}
    assert store.latest("000300")["snapshot_id"] == second
