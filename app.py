import os
import json
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
MessageEvent, ImageMessage, TextMessage,
TextSendMessage, ImageSendMessage
)

app = Flask(**name**)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get(“LINE_CHANNEL_ACCESS_TOKEN”, “”)
LINE_CHANNEL_SECRET = os.environ.get(“LINE_CHANNEL_SECRET”, “”)
WORK_START_HOUR = int(os.environ.get(“WORK_START_HOUR”, “9”))
WORK_START_MINUTE = int(os.environ.get(“WORK_START_MINUTE”, “0”))

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ─── DB Setup ────────────────────────────────────────────────────────────────

def get_db():
conn = sqlite3.connect(“checkin.db”)
conn.row_factory = sqlite3.Row
return conn

def init_db():
with get_db() as db:
db.execute(”””
CREATE TABLE IF NOT EXISTS checkins (
id          INTEGER PRIMARY KEY AUTOINCREMENT,
user_id     TEXT NOT NULL,
display_name TEXT,
checkin_time TEXT NOT NULL,
late_minutes INTEGER NOT NULL DEFAULT 0,
km_owed     REAL NOT NULL DEFAULT 0,
deadline    TEXT,
completed   INTEGER NOT NULL DEFAULT 0,
date        TEXT NOT NULL
)
“””)
db.execute(”””
CREATE TABLE IF NOT EXISTS run_reports (
id          INTEGER PRIMARY KEY AUTOINCREMENT,
checkin_id  INTEGER NOT NULL,
user_id     TEXT NOT NULL,
report_time TEXT NOT NULL,
km_reported REAL NOT NULL DEFAULT 0,
FOREIGN KEY (checkin_id) REFERENCES checkins(id)
)
“””)
db.execute(”””
CREATE TABLE IF NOT EXISTS user_settings (
user_id     TEXT PRIMARY KEY,
work_start_hour   INTEGER NOT NULL DEFAULT 9,
work_start_minute INTEGER NOT NULL DEFAULT 0,
display_name TEXT,
created_at  TEXT NOT NULL
)
“””)
db.commit()

init_db()

# ─── Helpers ─────────────────────────────────────────────────────────────────

def calc_km(late_minutes):
if late_minutes <= 0:
return 0
return min(late_minutes, 15)

def get_display_name(user_id):
try:
profile = line_bot_api.get_profile(user_id)
return profile.display_name
except Exception:
return “成員”

def get_user_work_time(user_id):
“”“取得使用者的上班時間，若無設定則使用預設值”””
with get_db() as db:
row = db.execute(
“SELECT work_start_hour, work_start_minute FROM user_settings WHERE user_id=?”,
(user_id,)
).fetchone()

```
if row:
    return row["work_start_hour"], row["work_start_minute"]
else:
    # 沒有設定過，使用預設值
    return WORK_START_HOUR, WORK_START_MINUTE
```

def set_user_work_time(user_id, hour, minute):
“”“設定使用者的上班時間”””
display_name = get_display_name(user_id)
with get_db() as db:
db.execute(
“”“INSERT OR REPLACE INTO user_settings
(user_id, work_start_hour, work_start_minute, display_name, created_at)
VALUES (?, ?, ?, ?, ?)”””,
(user_id, hour, minute, display_name, datetime.now().isoformat())
)
db.commit()

def get_today():
return datetime.now().strftime(”%Y-%m-%d”)

def already_checked_in_today(user_id):
today = get_today()
with get_db() as db:
row = db.execute(
“SELECT id FROM checkins WHERE user_id=? AND date=?”,
(user_id, today)
).fetchone()
return row is not None

def get_pending_debt(user_id):
“”“Return list of incomplete checkins with km still owed.”””
with get_db() as db:
rows = db.execute(
“”“SELECT c.id, c.km_owed, c.deadline, c.date,
COALESCE(SUM(r.km_reported), 0) as km_done
FROM checkins c
LEFT JOIN run_reports r ON r.checkin_id = c.id
WHERE c.user_id=? AND c.completed=0 AND c.km_owed > 0
GROUP BY c.id”””,
(user_id,)
).fetchall()
return rows

def check_overdue_and_penalize():
“”“Add +3K to debts that passed deadline and mark them penalized.”””
now = datetime.now().isoformat()
with get_db() as db:
overdue = db.execute(
“”“SELECT c.id, c.user_id, c.km_owed, c.display_name,
COALESCE(SUM(r.km_reported), 0) as km_done
FROM checkins c
LEFT JOIN run_reports r ON r.checkin_id = c.id
WHERE c.completed=0 AND c.km_owed > 0 AND c.deadline < ?
GROUP BY c.id”””,
(now,)
).fetchall()

```
    for row in overdue:
        remaining = row["km_owed"] - row["km_done"]
        if remaining > 0:
            new_deadline = (datetime.now() + timedelta(days=2)).isoformat()
            new_km = row["km_owed"] + 3
            db.execute(
                "UPDATE checkins SET km_owed=?, deadline=? WHERE id=?",
                (new_km, new_deadline, row["id"])
            )
            db.commit()
            # Notify the user in whatever group they're in (best effort)
            try:
                line_bot_api.push_message(
                    row["user_id"],
                    TextSendMessage(
                        text=f"⚠️ {row['display_name']}，你有罰跑逾期未完成！\n"
                             f"已加罰 +3K，新期限 2 天內完成。\n"
                             f"總欠債：{new_km:.1f} K，加油！"
                    )
                )
            except Exception:
                pass
```

# ─── Webhook ─────────────────────────────────────────────────────────────────

@app.route(”/callback”, methods=[“POST”])
def callback():
signature = request.headers.get(“X-Line-Signature”, “”)
body = request.get_data(as_text=True)
try:
handler.handle(body, signature)
except InvalidSignatureError:
abort(400)
return “OK”

# ─── Image Message → 打卡 ─────────────────────────────────────────────────────

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
user_id = event.source.user_id
now = datetime.now()
today = now.strftime(”%Y-%m-%d”)

```
# ── 測試期間暫時關閉重複打卡檢查，測完後再開啟 ──
# if already_checked_in_today(user_id):
#     line_bot_api.reply_message(
#         event.reply_token,
#         TextSendMessage(text="你今天已經打過卡囉 ✅")
#     )
#     return

display_name = get_display_name(user_id)

# 取得使用者的上班時間設定
work_hour, work_minute = get_user_work_time(user_id)

work_start = now.replace(hour=work_hour, minute=work_minute, second=0, microsecond=0)
late_seconds = (now - work_start).total_seconds()
late_minutes = max(0, int(late_seconds / 60))
km = calc_km(late_minutes)
deadline = (now + timedelta(days=2)).isoformat() if km > 0 else None

with get_db() as db:
    db.execute(
        """INSERT INTO checkins
           (user_id, display_name, checkin_time, late_minutes, km_owed, deadline, completed, date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, display_name, now.isoformat(), late_minutes, km, deadline, 0 if km > 0 else 1, today)
    )
    db.commit()

# 回覆
if late_minutes == 0:
    reply = (
        f"✅ {display_name} 打卡成功！\n"
        f"—— {now.strftime('%H:%M:%S')} ——\n"
        f"\n"
        f"今天也是準時上班的社畜 🦦"
    )
else:
    capped = late_minutes >= 15
    reply = (
        f"🛎️ {display_name} 打卡成功！\n"
        f"—— {now.strftime('%H:%M:%S')} ——\n"
        f"\n"
        f"你是遲到仔 🫵🏻 今天晚到 {late_minutes} 分鐘\n"
        f"罰你跑步 💥{km}公里💥\n"
        f"給我在（{(now + timedelta(days=2))} 前）跑完！\n"
        f"\n"
        f"跑完請回覆「我跑完了」\n"
        f"否則晚一天多3K👻"
    )

line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

# 順便檢查有沒有人逾期
check_overdue_and_penalize()
```

# ─── Text Message → 指令 ──────────────────────────────────────────────────────

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
user_id = event.source.user_id
text = event.message.text.strip()

```
# ── 設定上班時間：「上班時間 9:30」或「上班 9 30」──
if text.startswith("上班時間") or text.startswith("設定上班時間"):
    import re
    # 嘗試匹配各種格式：9:30、9 30、9時30分
    match = re.search(r"(\d{1,2})[:\s時](\d{1,2})", text)
    if not match:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="請輸入正確的格式：\n例如「上班時間 9:30」或「上班時間 9 30」\n\n(小時數需在 0-23，分鐘數在 0-59)")
        )
        return
    
    hour = int(match.group(1))
    minute = int(match.group(2))
    
    # 驗證時間範圍
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="❌ 時間格式不對！\n小時數：0-23\n分鐘數：0-59")
        )
        return
    
    # 保存設定
    set_user_work_time(user_id, hour, minute)
    display_name = get_display_name(user_id)
    
    reply = (
        f"✅ {display_name} 的上班時間已設定\n"
        f"⏰ {hour:02d}:{minute:02d}\n\n"
        f"之後打卡會以此時間計算遲到。"
    )
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
    return

# ── 查看自己的上班時間設定 ──
if text in ["我的設定", "我的上班時間", "查看設定"]:
    hour, minute = get_user_work_time(user_id)
    display_name = get_display_name(user_id)
    reply = (
        f"📋 {display_name} 的上班時間設定\n"
        f"⏰ {hour:02d}:{minute:02d}\n\n"
        f"輸入「上班時間 時:分」可修改設定"
    )
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
    return

# ── 查欠債 ──
if text in ["欠債", "我的欠債", "查詢"]:
    debts = get_pending_debt(user_id)
    if not debts:
        reply = "你目前沒有未完成的罰跑 🎉"
    else:
        lines = ["📋 你的罰跑欠債："]
        total_remaining = 0
        for d in debts:
            remaining = max(0, d["km_owed"] - d["km_done"])
            total_remaining += remaining
            deadline_str = datetime.fromisoformat(d["deadline"]).strftime("%m/%d %H:%M") if d["deadline"] else "—"
            lines.append(f"・{d['date']} 遲到 → 剩 {remaining:.1f}K（期限 {deadline_str}）")
        lines.append(f"\n總計剩餘：{total_remaining:.1f} K")
        reply = "\n".join(lines)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

# ── 全員欠債排行 ──
elif text in ["排行", "欠債排行", "誰欠最多"]:
    with get_db() as db:
        rows = db.execute(
            """SELECT c.display_name,
                      SUM(c.km_owed) - COALESCE(SUM(r.km_reported), 0) as remaining
               FROM checkins c
               LEFT JOIN run_reports r ON r.checkin_id = c.id
               WHERE c.completed=0 AND c.km_owed > 0
               GROUP BY c.user_id
               ORDER BY remaining DESC"""
        ).fetchall()
    if not rows:
        reply = "目前大家都沒有未完成的罰跑 🎉"
    else:
        medals = ["🥇", "🥈", "🥉"]
        lines = ["🏆 罰跑欠債排行："]
        for i, r in enumerate(rows):
            medal = medals[i] if i < 3 else f"{i+1}."
            lines.append(f"{medal} {r['display_name']}：{max(0, r['remaining']):.1f} K")
        reply = "\n".join(lines)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

# ── 說明 ──
elif text in ["說明", "help", "Help", "指令"]:
    reply = (
        "📖 打卡罰跑機器人\n\n"
        "【打卡】傳任何照片\n"
        "【回報跑步】傳跑步 App 截圖 + 輸入「跑完 X」\n\n"
        "【基本指令】\n"
        "・欠債 → 查自己的罰跑狀況\n"
        "・排行 → 全員欠債排名\n"
        "・說明 → 顯示此說明\n\n"
        "【個人設定】\n"
        "・上班時間 HH:MM → 設定自己的上班時間（例：上班時間 9:30）\n"
        "・我的設定 → 查看自己的上班時間設定\n\n"
        "【規則】\n"
        "・遲到幾分 = 跑幾K（最多15K）\n"
        "・2天內跑完，逾期 +3K\n"
    )
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

# ── 回報跑步：「跑完 5」或「跑完5k」──
elif text.startswith("跑完"):
    import re
    match = re.search(r"[\d.]+", text)
    if not match:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="請輸入格式：跑完 X（例如：跑完 5）")
        )
        return

    km_done = float(match.group())
    debts = get_pending_debt(user_id)
    if not debts:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="你目前沒有未完成的罰跑 🎉")
        )
        return

    # 填入最早一筆欠債
    oldest = debts[0]
    remaining_before = max(0, oldest["km_owed"] - oldest["km_done"])

    with get_db() as db:
        db.execute(
            "INSERT INTO run_reports (checkin_id, user_id, report_time, km_reported) VALUES (?, ?, ?, ?)",
            (oldest["id"], user_id, datetime.now().isoformat(), km_done)
        )
        # 更新 km_done
        new_done = oldest["km_done"] + km_done
        if new_done >= oldest["km_owed"]:
            db.execute("UPDATE checkins SET completed=1 WHERE id=?", (oldest["id"],))
        db.commit()

    remaining_after = max(0, oldest["km_owed"] - new_done)
    display_name = get_display_name(user_id)

    if remaining_after <= 0:
        reply = (
            f"🎉 {display_name} 完成罰跑！\n"
            f"跑了 {km_done:.1f} K，這筆欠債結清 ✅"
        )
    else:
        reply = (
            f"💪 {display_name} 回報 {km_done:.1f} K\n"
            f"這筆還剩 {remaining_after:.1f} K，繼續加油！"
        )
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
```

# ─── Main ─────────────────────────────────────────────────────────────────────

if **name** == “**main**”:
port = int(os.environ.get(“PORT”, 5000))
app.run(host=“0.0.0.0”, port=port)
