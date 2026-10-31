"""独立体验控制库迁移；不接触正式业务表。"""
import json


def migrate(db):
    version = db.execute("SELECT data FROM records WHERE id='experience-schema'").fetchone()
    if version and json.loads(version[0]).get("version", 0) >= 2:
        return
    for row in db.execute("SELECT id,data FROM records WHERE kind='grant'").fetchall():
        try:
            value = json.loads(row["data"])
        except (ValueError, TypeError):
            continue  # 原有隔离流程处理损坏记录。
        if not isinstance(value, dict):
            continue
        if "pages" not in value and isinstance(value.get("visible_pages"), list):
            value["pages"] = value["visible_pages"]
        value.setdefault("allow_chat_attachments", False)
        value.setdefault("allow_web_search", False)
        db.execute("UPDATE records SET data=? WHERE id=?", (json.dumps(value), row["id"]))
    db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?)",
               ("experience-schema", "schema", "global", json.dumps({"version": 2})))
