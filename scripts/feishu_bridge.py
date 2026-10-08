#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""feishu_bridge.py — 飞书 → Trinity 消息采集桥（2026-09-09）

长连接（WebSocket）接收 im.message.receive_v1 → 去重(msg_id) → Trinity ingest。
前置（飞书开放平台控制台）：机器人能力 + 事件订阅"长连接" + im.message.receive_v1 + im:message 权限。
"""
from __future__ import annotations
import json, logging, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("feishu-bridge")


def _creds() -> tuple:
    app_id = os.environ.get("FEISHU_APP_ID", "")
    secret = os.environ.get("FEISHU_APP_SECRET", "")
    if app_id and secret:
        return app_id, secret
    try:
        # t31：键缩进在版本化文件的 `refs` 下、值还带引号 ⇒ 用**行式 strip 解析**
        #（原来锚 `^KEY` 且不 strip ⇒ 恒不匹配 ⇒ 静默返回 ("", "")，只有 env 能生效）
        with open(os.path.expanduser(r"~\.dsh\.credentials.yaml"), encoding="utf-8-sig") as fh:
            for line in fh:
                if line.strip().startswith("FEISHU_APP_ID"):
                    app_id = line.strip().partition(":")[2].strip().strip("'\"") or app_id
                elif line.strip().startswith("FEISHU_APP_SECRET"):
                    secret = line.strip().partition(":")[2].strip().strip("'\"") or secret
        return app_id, secret
    except Exception:
        return "", ""


def _dedup(msg_id: str) -> bool:
    try:
        import psycopg2
        conn = psycopg2.connect(host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
            port=int(os.environ.get("TRINITY_PG_PORT", "5432")),
            dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
            user=os.environ.get("TRINITY_PG_USER", "trinity"),
            password=os.environ.get("TRINITY_PG_PASSWORD", ""), connect_timeout=3)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM memories WHERE metadata->>'feishu_msg_id' = %s LIMIT 1", (msg_id,))
        hit = cur.fetchone() is not None
        conn.close()
        return hit
    except Exception:
        return False


def _text_of(msg: dict) -> str:
    try:
        c = json.loads(msg.get("content") or "{}")
    except Exception:
        return ""
    if "text" in c:
        return str(c["text"])
    if "title" in c:
        return str(c["title"])
    return ""


def _handle(raw: dict) -> None:
    try:
        ev = raw.get("event") if "event" in raw else raw
        message = ev.get("message") or {}
        msg_id = message.get("message_id") or ""
        if not msg_id or message.get("message_type") != "text":
            return
        if _dedup(msg_id):
            return
        text = _text_of(message)
        if not text.strip():
            return
        chat_id = str(message.get("chat_id") or "")[:40]
        # 2026-09-09 修复：sender 提取不得对 str 调 .get
        _sender_ev = ev.get("sender")
        _sender_ev = _sender_ev if isinstance(_sender_ev, dict) else {}
        _sid = _sender_ev.get("sender_id") or {}
        _sid = _sid if isinstance(_sid, dict) else {}
        sender = str(_sid.get("open_id", "") or "")[:24]
        from trinity.core.client import Trinity
        Trinity().ingest(
            content="[feishu] " + text[:1500], category="chat", importance=0.45,
            tags=["im", "feishu", "chat:" + chat_id[:12]],
            agent_id="feishu-bridge",
            metadata={"provenance_role": "derived", "im_source": "feishu",
                      "feishu_msg_id": msg_id, "feishu_chat_id": chat_id,
                      "feishu_sender": sender, "im_sent_at": message.get("create_time", "")},
            postprocess=False)
        log.info("ingested %s (%s)", msg_id[:12], text[:40])
    except Exception as e:  # noqa: BLE001
        log.warning("handle failed: %s", str(e)[:160])


def main() -> int:
    app_id, secret = _creds()
    if not app_id or not secret:
        log.error("missing FEISHU_APP_ID/SECRET")
        return 1
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    def on_msg(data: P2ImMessageReceiveV1) -> None:
        print("HANDLER_CALLED type=", type(data).__name__, flush=True)
        try:
            _handle(json.loads(lark.JSON.marshal(data)))
        except Exception as ex:  # noqa: BLE001
            print("ON_MSG_ERR:", str(ex)[:300], flush=True)

    builder = lark.EventDispatcherHandler.builder("", "")
    builder.register_p2_im_message_receive_v1(on_msg)
    handler = builder.build()
    # 2026-09-09 诊断：DEBUG 可看到每个到达的原始事件帧
    client = lark.ws.Client(app_id, secret, event_handler=handler, log_level=lark.LogLevel.DEBUG)
    log.info("feishu-bridge connecting (app=%s)...", app_id)
    client.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
