"""Immutable, content-addressed index-fund observation snapshots."""

import hashlib
import json
from typing import Any

from ai_quant_assistant.storage.database import LocalDatabase


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class IndexFundSnapshotStore:
    def __init__(self, database: LocalDatabase):
        self.database = database

    def save(self, index_code: str, spec: dict, inputs: dict, result: dict) -> str:
        if not index_code.isascii() or len(index_code) != 6 or not index_code.isdigit():
            raise ValueError("invalid index code")
        spec_json, input_json, result_json = map(_canonical, (spec, inputs, result))
        digest = hashlib.sha256(_canonical([index_code, spec, inputs, result]).encode()).hexdigest()
        snapshot_id = "index-fund-" + digest
        self.database.initialize()
        with self.database.connect() as c:
            c.execute(
                "INSERT INTO index_fund_snapshot (snapshot_id,index_code,spec_json,input_json,result_json) VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                [snapshot_id, index_code, spec_json, input_json, result_json],
            )
        return snapshot_id

    def get(self, snapshot_id: str) -> dict:
        self.database.initialize()
        with self.database.connect(read_only=True) as c:
            row = c.execute(
                "SELECT index_code,spec_json,input_json,result_json,created_at FROM index_fund_snapshot WHERE snapshot_id=?",
                [snapshot_id],
            ).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        return {"snapshot_id": snapshot_id, "index_code": row[0], "spec": json.loads(row[1]),
                "inputs": json.loads(row[2]), "result": json.loads(row[3]), "created_at": row[4].isoformat()}

    def latest(self, index_code: str) -> dict | None:
        self.database.initialize()
        with self.database.connect(read_only=True) as c:
            # A repeated sync can reproduce an existing content-addressed snapshot.
            # Its original creation time is not the time of the latest publication.
            row = c.execute("""
                SELECT snapshot_id FROM (
                    SELECT snapshot_id, created_at AS published_at
                    FROM index_fund_snapshot WHERE index_code=?
                    UNION ALL
                    SELECT s.snapshot_id, t.completed_at AS published_at
                    FROM operation_task t JOIN index_fund_snapshot s
                      ON s.snapshot_id=json_extract_string(t.result_json, '$.snapshot_id')
                    WHERE s.index_code=? AND t.status='succeeded'
                      AND json_extract_string(t.spec_json, '$.dataset')='index_fund_observation'
                      AND t.completed_at IS NOT NULL
                ) ORDER BY published_at DESC, snapshot_id DESC LIMIT 1
                """, [index_code, index_code]).fetchone()
        return self.get(row[0]) if row else None
