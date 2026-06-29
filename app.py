import os
import re
import requests
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, ImageMessage, TextMessage,
    TextSendMessage
)

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
WORK_START_HOUR = int(os.environ.get("WORK_START_HOUR", "9"))
WORK_START_MINUTE = int(os.environ.get("WORK_START_MINUTE", "0"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_CHECKIN_DB = os.environ.get("NOTION_CHECKIN_DB", "")
NOTION_RUN_DB = os.environ.get("NOTION_RUN_DB", "")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

try:
    import pytz
    TZ = pytz.timezone("Asia/Taipei")
except ImportError:
    TZ = None

def now_tw():
    if TZ:
        return datetime.now(TZ).replace(tzinfo=None)
    return datetime.now()

# --- Notion ------------------------------------------------------------------

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

def notion_add_checkin(display_name, checkin_time, late_minutes, km_owed, deadline, date):
    """新增一筆打卡紀錄到 Notion"""
    if not NOTION_TOKEN or not NOTION_CHECKIN_DB:
        return None
    props = {
        "Name": {
            "title": [{"text": {"content": display_name}}]
        },
        "打卡時間": {
            "date": {"start": checkin_time}
        },
        "遲到分鐘": {
            "number": late_minutes
        },
        "罰跑K數": {
            "number": km_owed
        },
        "完成": {
            "checkbox": km_owed == 0
        },
        "日期": {
            "rich_text": [{"text": {"content": date}}]
        }
    }
    if deadline:
        props["截止日"] = {"date": {"start": deadline[:10]}}

    try:
        resp = requests.post(
            "https://api.notion.com/v1/pages",
            headers=NOTION_HEADERS,
            json={"parent": {"database_id": NOTION_CHECKIN_DB}, "properties": props},
            timeout=5
        )
        data = resp.json()
        return data.get("id")  # 回傳 Notion page id，之後跑步回報會用到
    except Exception:
        return None

def notion_complete_checkin(notion_page_id):
    """把打卡紀錄標記為完成"""
    if not NOTION_TOKEN or not notion_page_id:
        return
    try:
        requests.patch(
            f"https://api.notion.com/v1/pages/{notion_page_id}",
            headers=NOTION_HEADERS,
            json={"properties": {"完成": {"checkbox": True}}},
            timeout=5
        )
    except Exception:
        pass

def notion_delete_page(notion_page_id):
    """在 Notion 中刪除（封存）特定頁面"""
    if not NOTION_TOKEN or not notion_page_id:
        return
    try:
        requests.patch(
            f"https://api.notion.com/v1/pages/{notion_page_id}",
            headers=NOTION_HEADERS,
            json={"archived": True},
            timeout=5
        )
    except Exception:
        pass

def notion_add_run_report(display_name, report_time, km_done, notion_checkin_page_id):
    """新增一筆跑步回報到 Notion"""
    if not NOTION_TOKEN or not NOTION_RUN_DB:
        return
    props = {
        "Name": {
            "title": [{"text": {"content": display_name}}]
        },
        "回報時間": {
            "date": {"start": report_time}
        },
        "跑了幾K": {
            "number": km_done
        },
        "user_id": {
            "rich_text": [{"text": {"content": display_name}}]
        }
    }
    if notion_checkin_page_id:
        props["對應打卡"] = {
            "relation": [{"id": notion_checkin_page_id}]
        }
    try:
        requests.post(
            "https://api.notion.com/v1/pages",
            headers=NOTION_HEADERS,
            json={"parent": {"database_id": NOTION_RUN_DB}, "properties": props},
            timeout=5
        )
    except Exception:
        pass

# --- DB Setup ----------------------------------------------------------------

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS checkins (
                    id                  SERIAL PRIMARY KEY,
                    user_id             TEXT NOT NULL,
                    display_name        TEXT,
                    checkin_time        TEXT NOT NULL,
                    late_minutes        INTEGER NOT NULL DEFAULT 0,
                    km_owed             REAL NOT NULL DEFAULT 0,
                    deadline            TEXT,
                    completed           INTEGER NOT NULL DEFAULT 0,
                    date                TEXT NOT NULL,
                    notion_page_id      TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS run_reports (
                    id          SERIAL PRIMARY KEY,
                    checkin_id  INTEGER NOT NULL,
                    user_id     TEXT NOT NULL,
                    report_time TEXT NOT NULL,
                    km_reported REAL NOT NULL DEFAULT 0,
                    FOREIGN KEY (checkin_id) REFERENCES checkins(id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id           TEXT PRIMARY KEY,
                    work_start_hour   INTEGER NOT NULL DEFAULT 9,
                    work_start_minute INTEGER NOT NULL DEFAULT 0,
                    display_name      TEXT,
                    created_at        TEXT NOT NULL
                )
            """)
            # 如果舊資料表沒有 notion_page_id 欄位，補上去
            cur.execute("""
                ALTER TABLE checkins ADD COLUMN IF NOT EXISTS notion_page_id TEXT
            """)
        conn.commit()

init_db()

# --- Helpers -----------------------------------------------------------------

def calc_km(late_minutes):
    if late_minutes <= 0:
        return 0
    return min(late_minutes, 15)

def get_display_name(user_id, source=None):
    try:
        if source and source.type == "group":
            profile = line_bot_api.get_group_member_profile(source.group_id, user_id)
        elif source and source.type == "room":
            profile = line_bot_api.get_room_member_profile(source.room_id, user_id)
        else:
            profile = line_bot_api.get_profile(user_id)
        return profile.display_name
    except Exception:
        return "成員"

def get_user_work_time(user_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT work_start_hour, work_start_minute FROM user_settings WHERE user_id=%s",
                (user_id,)
            )
            row = cur.fetchone()
    if row:
        return row["work_start_hour"], row["work_start_minute"]
    return WORK_START_HOUR, WORK_START_MINUTE

def set_user_work_time(user_id, hour, minute, source=None):
    display_name = get_display_name(user_id, source)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_settings
                   (user_id, work_start_hour, work_start_minute, display_name, created_at)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE
                   SET work_start_hour=%s, work_start_minute=%s, display_name=%s""",
                (user_id, hour, minute, display_name, now_tw().isoformat(),
                 hour, minute, display_name)
            )
        conn.commit()

def get_today():
    return now_tw().strftime("%Y-%m-%d")

def already_checked_in_today(user_id):
    today = get_today()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM checkins WHERE user_id=%s AND date=%s",
                (user_id, today)
            )
            row = cur.fetchone()
    return row is not None

def get_pending_debt(user_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.id, c.km_owed, c.deadline, c.date, c.notion_page_id,
                          COALESCE(SUM(r.km_reported), 0) as km_done
                   FROM checkins c
                   LEFT JOIN run_reports r ON r.checkin_id = c.id
                   WHERE c.user_id=%s AND c.completed=0 AND c.km_owed > 0
                   GROUP BY c.id, c.km_owed, c.deadline, c.date, c.notion_page_id""",
                (user_id,)
            )
            rows = cur.fetchall()
    return rows

def check_overdue_and_penalize():
    now = now_tw().isoformat()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.id, c.user_id, c.km_owed, c.display_name,
                          COALESCE(SUM(r.km_reported), 0) as km_done
                   FROM checkins c
                   LEFT JOIN run_reports r ON r.checkin_id = c.id
                   WHERE c.completed=0 AND c.km_owed > 0 AND c.deadline < %s
                   GROUP BY c.id, c.user_id, c.km_owed, c.display_name""",
                (now,)
            )
            overdue = cur.fetchall()

        for row in overdue:
            remaining = row["km_owed"] - row["km_done"]
            if remaining > 0:
                new_deadline = (now_tw() + timedelta(days=2)).isoformat()
                new_km = row["km_owed"] + 3
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE checkins SET km_owed=%s, deadline=%s WHERE id=%s",
                        (new_km, new_deadline, row["id"])
                    )
                conn.commit()
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

# --- Webhook -----------------------------------------------------------------

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# --- Image Message → 打卡 -----------------------------------------------------

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    user_id = event.source.user_id
    now = now_tw()
    today = now.strftime("%Y-%m-%d")

    if already_checked_in_today(user_id):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="你今天已經打過卡囉 ✅")
        )
        return

    display_name = get_display_name(user_id, event.source)
    work_hour, work_minute = get_user_work_time(user_id)
    work_start = now.replace(hour=work_hour, minute=work_minute, second=0, microsecond=0)
    late_seconds = (now - work_start).total_seconds()
    late_minutes = max(0, int(late_seconds / 60))
    km = calc_km(late_minutes)
    deadline = (now + timedelta(days=2)).isoformat() if km > 0 else None

    # 寫入 Notion
    notion_page_id = notion_add_checkin(
        display_name=display_name,
        checkin_time=now.isoformat(),
        late_minutes=late_minutes,
        km_owed=km,
        deadline=deadline,
        date=today
    )

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO checkins
                   (user_id, display_name, checkin_time, late_minutes, km_owed, deadline, completed, date, notion_page_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (user_id, display_name, now.isoformat(), late_minutes, km, deadline,
                 0 if km > 0 else 1, today, notion_page_id)
            )
        conn.commit()

    if late_minutes == 0:
        reply = (
            f"✅ {display_name} 打卡成功！\n"
            f"—— {now.strftime('%H:%M:%S')} ——\n\n"
            f"今天也是準時上班的社畜 🦦"
        )
    else:
        reply = (
            f"🛎️ {display_name} 打卡成功！\n"
            f"—— {now.strftime('%H:%M:%S')} ——\n\n"
            f"你是遲到仔 🫵🏻 今天晚到 {late_minutes} 分鐘\n"
            f"罰你跑步 💥{km}公里💥\n"
            f"給我在 {(now + timedelta(days=2)).strftime('%Y/%m/%d')} 前跑完！\n\n"
            f"跑完請回覆「跑完 X」\n"
            f"否則晚一天多3K👻"
        )

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
    check_overdue_and_penalize()

# --- Text Message → 指令 ------------------------------------------------------

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    # 重置今天打卡資料
    if text == "重置今天":
        today = get_today()
        display_name = get_display_name(user_id, event.source)
        
        with get_db() as conn:
            with conn.cursor() as cur:
                # 1. 查找今天該使用者是否有打卡紀錄
                cur.execute(
                    "SELECT id, notion_page_id FROM checkins WHERE user_id=%s AND date=%s ORDER BY id DESC LIMIT 1",
                    (user_id, today)
                )
                row = cur.fetchone()
                
                if not row:
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage(text="🤷‍♂️ 你今天本來就沒有打卡紀錄喔！")
                    )
                    return
                
                checkin_id = row["id"]
                notion_page_id = row["notion_page_id"]
                
                # 2. 先刪除 run_reports 相關關聯跑步回報 (避免外鍵約束錯誤)
                cur.execute("DELETE FROM run_reports WHERE checkin_id=%s", (checkin_id,))
                
                # 3. 刪除打卡紀錄
                cur.execute("DELETE FROM checkins WHERE id=%s", (checkin_id,))
            conn.commit()
            
        # 4. 同步將 Notion 打卡資料刪除 (移至垃圾桶)
        if notion_page_id:
            notion_delete_page(notion_page_id)
            
        reply = f"🗑️ 已成功重置 {display_name} 今日的打卡紀錄與跑步回報！你現在可以重新傳照片打卡了。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # 設定上班時間
    if text.startswith("上班時間") or text.startswith("設定上班時間"):
        match = re.search(r"(\d{1,2})[:\s時](\d{1,2})", text)
        if not match:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="請輸入正確的格式：\n例如「上班時間 9:30」或「上班時間 9 30」\n\n(小時數需在 0-23，分鐘數在 0-59)")
            )
            return
        hour = int(match.group(1))
        minute = int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="❌ 時間格式不對！\n小時數：0-23\n分鐘數：0-59")
            )
            return
        set_user_work_time(user_id, hour, minute, event.source)
        display_name = get_display_name(user_id, event.source)
        reply = (
            f"✅ {display_name} 的上班時間已設定\n"
            f"⏰ {hour:02d}:{minute:02d}\n\n"
            f"之後打卡會以此時間計算遲到。"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # 查看上班時間設定
    if text in ["我的設定", "我的上班時間", "查看設定"]:
        hour, minute = get_user_work_time(user_id)
        display_name = get_display_name(user_id, event.source)
        reply = (
            f"📋 {display_name} 的上班時間設定\n"
            f"⏰ {hour:02d}:{minute:02d}\n\n"
            f"輸入「上班時間 時:分」可修改設定"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # 顯示所有成員
    if text in ["成員", "所有成員", "成員列表"]:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT display_name FROM checkins ORDER BY display_name")
                rows = cur.fetchall()
        if not rows:
            reply = "目還沒有任何打卡紀錄 📭"
        else:
            lines = ["👥 有紀錄的成員："]
            for r in rows:
                lines.append(f"・{r['display_name']}")
            reply = "\n".join(lines)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # 我的打卡紀錄
    if text in ["我的紀錄", "打卡紀錄", "紀錄"]:
        weekday_map = ["一", "二", "三", "四", "五", "六", "日"]
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT checkin_time, late_minutes, km_owed
                       FROM checkins
                       WHERE user_id=%s
                       ORDER BY checkin_time DESC
                       LIMIT 10""",
                    (user_id,)
                )
                rows = cur.fetchall()
        if not rows:
            reply = "你還沒有任何打卡紀錄 📭"
        else:
            lines = ["📋 你的近10次打卡紀錄："]
            for r in rows:
                dt = datetime.fromisoformat(r["checkin_time"])
                weekday = weekday_map[dt.weekday()]
                time_str = dt.strftime(f"%m/%d(週{weekday}) %H:%M:%S")
                if r["late_minutes"] == 0:
                    status = "✅ 準時"
                else:
                    status = f"🕐 遲到 {r['late_minutes']} 分／罰 {r['km_owed']:.1f}K"
                lines.append(f"・{time_str} {status}")
            reply = "\n".join(lines)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # 補打卡
    if text.startswith("補打卡") or text.startswith("調整打卡"):
        match = re.search(r"(\d{1,2}):(\d{2})", text)
        if not match:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="請輸入格式：補打卡 HH:MM\n例如：補打卡 09:05\n\n限上班時間後 1 小時內")
            )
            return

        hour = int(match.group(1))
        minute = int(match.group(2))
        now = now_tw()
        work_hour, work_minute = get_user_work_time(user_id)

        target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        work_start = now.replace(hour=work_hour, minute=work_minute, second=0, microsecond=0)
        limit_time = work_start + timedelta(hours=1)

        if not (work_start <= target_time <= limit_time):
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=f"❌ 補打卡時間不在允許範圍內\n"
                         f"只能填寫 {work_start.strftime('%H:%M')} ~ {limit_time.strftime('%H:%M')} 之間的時間"
                )
            )
            return

        today = now.strftime("%Y-%m-%d")
        late_seconds = (target_time - work_start).total_seconds()
        late_minutes = max(0, int(late_seconds / 60))
        km = calc_km(late_minutes)
        deadline = (now + timedelta(days=2)).isoformat() if km > 0 else None
        display_name = get_display_name(user_id, event.source)

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, notion_page_id FROM checkins WHERE user_id=%s AND date=%s",
                    (user_id, today)
                )
                existing = cur.fetchone()
                if existing:
                    # 更新 Notion
                    if existing["notion_page_id"]:
                        try:
                            requests.patch(
                                f"https://api.notion.com/v1/pages/{existing['notion_page_id']}",
                                headers=NOTION_HEADERS,
                                json={"properties": {
                                    "打卡時間": {"date": {"start": target_time.isoformat()}},
                                    "遲到分鐘": {"number": late_minutes},
                                    "罰跑K數": {"number": km},
                                    "完成": {"checkbox": km == 0},
                                    **({"截止日": {"date": {"start": deadline[:10]}}} if deadline else {})
                                }},
                                timeout=5
                            )
                        except Exception:
                            pass
                    cur.execute(
                        """UPDATE checkins
                           SET checkin_time=%s, late_minutes=%s, km_owed=%s, deadline=%s, completed=%s
                           WHERE id=%s""",
                        (target_time.isoformat(), late_minutes, km, deadline, 0 if km > 0 else 1, existing["id"])
                    )
                else:
                    notion_page_id = notion_add_checkin(
                        display_name=display_name,
                        checkin_time=target_time.isoformat(),
                        late_minutes=late_minutes,
                        km_owed=km,
                        deadline=deadline,
                        date=today
                    )
                    cur.execute(
                        """INSERT INTO checkins
                           (user_id, display_name, checkin_time, late_minutes, km_owed, deadline, completed, date, notion_page_id)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (user_id, display_name, target_time.isoformat(), late_minutes, km, deadline,
                         0 if km > 0 else 1, today, notion_page_id)
                    )
            conn.commit()

        if late_minutes == 0:
            reply = (
                f"✅ {display_name} 補打卡成功！\n"
                f"—— {target_time.strftime('%H:%M:%S')} ——\n\n"
                f"今天也是準時上班的社畜 🦦"
            )
        else:
            reply = (
                f"🛎️ {display_name} 補打卡成功！\n"
                f"—— {target_time.strftime('%H:%M:%S')} ——\n\n"
                f"你是遲到仔 🫵🏻 今天晚到 {late_minutes} 分鐘\n"
                f"罰你跑步 💥{km}公里💥\n"
                f"給我在 {(now + timedelta(days=2)).strftime('%Y/%m/%d')} 前跑完！\n\n"
                f"跑完請回覆「跑完 X」\n"
                f"否則晚一天多3K👻"
            )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # 查欠債
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
        return

    # 全員欠債排行
    if text in ["排行", "欠債排行", "誰欠最多"]:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT c.display_name,
                              SUM(c.km_owed) - COALESCE(SUM(r.km_reported), 0) as remaining
                       FROM checkins c
                       LEFT JOIN run_reports r ON r.checkin_id = c.id
                       WHERE c.completed=0 AND c.km_owed > 0
                       GROUP BY c.user_id, c.display_name
                       ORDER BY remaining DESC"""
                )
                rows = cur.fetchall()
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
        return

    # 說明
    if text in ["說明", "help", "Help", "指令"]:
        reply = (
            "📖 打卡罰跑機器人\n\n"
            "【打卡】傳任何照片\n"
            "【補打卡】補打卡 HH:MM（限上班後1小時內）\n"
            "【重置今天】清除自己當天的打卡與跑步紀錄\n\n"
            "【基本指令】\n"
            "・欠債 → 查自己的罰跑狀況\n"
            "・排行 → 全員欠債排名\n"
            "・成員 → 查看所有有紀錄的成員\n"
            "・我的紀錄 → 近10次打卡紀錄\n"
            "・說明 → 顯示此說明\n\n"
            "【個人設定】\n"
            "・上班時間 HH:MM → 設定自己的上班時間（例：上班時間 9:30）\n"
            "・我的設定 → 查看自己的上班時間設定\n\n"
            "【規則】\n"
            "・遲到幾分 = 跑幾K（最多15K）\n"
            "・2天內跑完，逾期 +3K\n"
            "（若要徹底清除某天錯誤資料可輸入「重置今天」）"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # 回報跑步
    if any(text.startswith(kw) for kw in ["跑完", "完成", "結束", "done"]):
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

        oldest = debts[0]
        display_name = get_display_name(user_id, event.source)

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO run_reports (checkin_id, user_id, report_time, km_reported) VALUES (%s, %s, %s, %s)",
                    (oldest["id"], user_id, now_tw().isoformat(), km_done)
                )
                new_done = oldest["km_done"] + km_done
                is_completed = new_done >= oldest["km_owed"]
                if is_completed:
                    cur.execute("UPDATE checkins SET completed=1 WHERE id=%s", (oldest["id"],))
            conn.commit()

        # 寫入 Notion 跑步回報
        notion_add_run_report(
            display_name=display_name,
            report_time=now_tw().isoformat(),
            km_done=km_done,
            notion_checkin_page_id=oldest.get("notion_page_id")
        )

        # 如果完成，把 Notion 打卡紀錄標記完成
        if is_completed and oldest.get("notion_page_id"):
            notion_complete_checkin(oldest["notion_page_id"])

        remaining_after = max(0, oldest["km_owed"] - new_done)

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
        return

# --- Main --------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
