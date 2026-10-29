from fastapi import APIRouter, HTTPException, Request

from ai_quant_assistant.storage.index_fund_snapshots import IndexFundSnapshotStore

INDEXES = {
    "000300": "沪深300", "000016": "上证50", "000905": "中证500",
    "000852": "中证1000", "932000": "中证2000", "000510": "中证A500",
    "399006": "创业板指", "000688": "科创50",
}


def index_fund_router(database, templates, asset_version, actor):
    router = APIRouter(tags=["index-fund-observation"])
    store = IndexFundSnapshotStore(database)

    @router.get("/market/index-funds", include_in_schema=False)
    def page(request: Request):
        return templates.TemplateResponse(request=request, name="index-funds.html", context={"asset_version": asset_version, "username": actor})

    @router.get("/api/v1/index-funds/{index_code}")
    def latest(index_code: str):
        if index_code not in INDEXES:
            raise HTTPException(404, "不支持的指数")
        if not database.path.is_file():
            return {"index_code": index_code, "index_name": INDEXES[index_code], "snapshot": None}
        return {"index_code": index_code, "index_name": INDEXES[index_code], "snapshot": store.latest(index_code)}

    return router
