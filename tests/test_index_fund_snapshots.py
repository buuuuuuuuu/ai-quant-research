import json

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


def test_republication_of_existing_snapshot_becomes_latest(tmp_path):
    db = LocalDatabase(tmp_path / 'db.duckdb', DataLayout.from_root(tmp_path / 'data'))
    store = IndexFundSnapshotStore(db)
    first = store.save('000300', {'group': ['a']}, {}, {'points': [1]})
    second = store.save('000300', {'group': ['b']}, {}, {'points': [2]})
    with db.connect() as c:
        c.execute("UPDATE index_fund_snapshot SET created_at='2020-01-01' WHERE snapshot_id=?", [first])
        c.execute("UPDATE index_fund_snapshot SET created_at='2021-01-01' WHERE snapshot_id=?", [second])
        c.execute("""INSERT INTO operation_task (task_id,task_type,status,progress,spec_json,result_json,completed_at)
                     VALUES ('task-republish','sync','succeeded',100,?,?,'2022-01-01')""",
                  [json.dumps({'dataset': 'index_fund_observation'}), json.dumps({'snapshot_id': first})])
    assert store.latest('000300')['snapshot_id'] == first
