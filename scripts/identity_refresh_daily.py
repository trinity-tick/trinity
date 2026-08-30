# -*- coding: utf-8 -*-
"""每日全局自我更新（EXECUTION 174）——重算并写回 self-identity 记忆。

跨会话持续自我每日演进：关注领域/情绪基调/领悟随新自省更新。
"""
import sys, os, json

def main():
    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
    os.environ.setdefault("PGPASSWORD", "trinity")
    from trinity.brain.self_model import global_identity_to_memory
    ok = global_identity_to_memory(None)
    print(json.dumps({"updated": ok}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
