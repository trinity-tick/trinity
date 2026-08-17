#!/usr/bin/env python3
"""
Trinity REST API Server — memory market routes (/market/*).
"""

from typing import Optional

from fastapi import APIRouter, HTTPException

from trinity.market import (
    OrderBook,
    ReputationEngine,
    TrustExchange,
    create_asset,
    estimate_value,
    get_market_price,
)

from ._models import (
    MarketBuyRequest,
    MarketDelistRequest,
    MarketEndorseRequest,
    MarketListRequest,
    MarketPriceRequest,
    MarketReportRequest,
)

router = APIRouter()


_market_orderbook: Optional[OrderBook] = None
_market_exchange: Optional[TrustExchange] = None
_market_reputation: Optional[ReputationEngine] = None


def _get_orderbook() -> OrderBook:
    global _market_orderbook
    if _market_orderbook is None:
        _market_orderbook = OrderBook()
    return _market_orderbook


def _get_reputation() -> ReputationEngine:
    global _market_reputation
    if _market_reputation is None:
        _market_reputation = ReputationEngine()
    return _market_reputation


def _get_exchange() -> TrustExchange:
    global _market_exchange
    if _market_exchange is None:
        _market_exchange = TrustExchange(
            orderbook=_get_orderbook(),
            reputation=_get_reputation(),
        )
    return _market_exchange


@router.post("/market/list", tags=["Memory Market"],
          summary="挂单 —将记忆资产列表到市场")
async def market_list(req: MarketListRequest):
    """将一条记忆封装为 MemoryAsset 并挂单到订单簿。"""
    try:
        asset = create_asset(req.memory, req.owner, price=req.price, license=req.license)
        entry = _get_orderbook().list_asset(asset, price=req.price, currency=req.currency)
        return {
            "status": "listed",
            "asset_id": asset.memory_id,
            "owner_agent": asset.owner_agent,
            "price": entry.price,
            "currency": entry.currency,
            "license": asset.license,
            "listed_at": entry.listed_at,
        }
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/market/delist", tags=["Memory Market"],
          summary="撤单 —从订单簿移除挂单")
async def market_delist(req: MarketDelistRequest):
    """撤下一个已挂单的资产。"""
    ok = _get_orderbook().delist_asset(req.asset_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Asset {req.asset_id} not found or already delisted")
    return {"status": "delisted", "asset_id": req.asset_id}


@router.get("/market/search", tags=["Memory Market"],
         summary="搜索市场 —按关键词/模态价格搜索可用资产")
async def market_search(
    query: str = "",
    modality: Optional[str] = None,
    max_price: Optional[float] = None,
):
    """搜索当前订单簿中的活跃挂单。"""
    results = _get_orderbook().search_market(
        query=query,
        modality=modality,
        max_price=max_price,
    )
    return {
        "count": len(results),
        "results": [
            {
                "asset_id": e.asset_id,
                "owner_agent": e.asset.owner_agent,
                "modality": e.asset.modality,
                "tags": e.asset.tags,
                "price": e.price,
                "currency": e.currency,
                "license": e.asset.license,
                "listed_at": e.listed_at,
            }
            for e in results
        ],
    }


@router.get("/market/orderbook", tags=["Memory Market"],
         summary="订单簿—查看全部活跃挂单")
async def market_orderbook():
    """返回当前全部活跃挂单。"""
    return {
        "count": len(_get_orderbook()._orders),
        "orders": _get_orderbook().get_order_book(),
    }


@router.post("/market/buy", tags=["Memory Market"],
          summary="购买 —以信任货币购买记忆资产")
async def market_buy(req: MarketBuyRequest):
    """原子交易：验证余额→转账 →撤单 →记录交易。"""
    try:
        tx = _get_exchange().buy_asset(
            buyer_agent=req.buyer_agent,
            asset_id=req.asset_id,
            offer_price=req.offer_price,
            currency=req.currency,
        )
        return {
            "status": "completed",
            "tx_id": tx.tx_id,
            "buyer_agent": tx.buyer_agent,
            "seller_agent": tx.seller_agent,
            "asset_id": tx.asset_id,
            "price": tx.price,
            "currency": tx.currency,
            "timestamp": tx.timestamp,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/market/transactions/{agent_id}", tags=["Memory Market"],
         summary="交易历史 —查询 Agent 的历史交易记录")
async def market_transactions(agent_id: str, limit: int = 50):
    """返回指定 Agent 参与的所有交易记录。"""
    history = _get_exchange().get_transaction_history(agent_id, limit=limit)
    return {"agent_id": agent_id, "count": len(history), "transactions": history}


@router.get("/market/reputation/{agent_id}", tags=["Memory Market"],
         summary="声誉查询 —获取 Agent 的声誉详情")
async def market_reputation(agent_id: str):
    """返回 Agent 的多维度声誉分数和账本事件。"""
    score = _get_reputation().calculate_reputation(agent_id)
    ledger = _get_reputation().get_reputation_ledger(agent_id)
    return {
        "reputation": score.to_dict(),
        "ledger_events": len(ledger),
        "ledger": ledger,
    }


@router.post("/market/endorse", tags=["Memory Market"],
          summary="背书 —Agent 背书（信任投票）")
async def market_endorse(req: MarketEndorseRequest):
    """一个Agent 为另一个Agent 背书，提升其声誉分。"""
    entry = _get_reputation().endorse_agent(
        from_agent=req.from_agent,
        to_agent=req.to_agent,
        reason=req.reason,
    )
    return {
        "status": "endorsed",
        "event_id": entry.event_id,
        "from_agent": req.from_agent,
        "to_agent": req.to_agent,
        "timestamp": entry.timestamp,
    }


@router.post("/market/report", tags=["Memory Market"],
          summary="举报 —Agent 举报不良行为")
async def market_report(req: MarketReportRequest):
    """一个Agent 举报另一个Agent 的不良行为，降低其声誉分。"""
    entry = _get_reputation().report_agent(
        from_agent=req.from_agent,
        to_agent=req.to_agent,
        reason=req.reason,
    )
    return {
        "status": "reported",
        "event_id": entry.event_id,
        "from_agent": req.from_agent,
        "to_agent": req.to_agent,
        "timestamp": entry.timestamp,
    }


@router.get("/market/price/{modality}", tags=["Memory Market"],
         summary="市场均价 —查询某类模态的市场均价")
async def market_price(modality: str):
    """返回指定模态从历史交易中计算的市场均价。"""
    price = get_market_price(modality, hist_trades=None)
    return {"modality": modality, "average_price": price}


@router.post("/market/estimate", tags=["Memory Market"],
          summary="记忆估值—使用定价引擎估值一条记忆")
async def market_estimate(req: MarketPriceRequest):
    """基于稀有度、新鲜度、关联度和历史成交价估算记忆价值。"""
    orderbook_entries = _get_orderbook().get_order_book()
    value = estimate_value(
        memory=req.memory,
        market_data=orderbook_entries,
        hist_trades=None,
    )
    return {
        "estimated_value": value,
        "modality": req.memory.get("category", "text"),
    }


