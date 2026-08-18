import base64
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape, unescape
from pathlib import Path
"""
context_generator.py
用于将机器人保存的聊天记录整理为可视化html
使用方法：复制到data目录下 直接运行 生成main.html
媒体文件为相对引用
"""

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "message_index.json"
MEMBERSHIP_EVENTS_FILE = BASE_DIR / "membership_events.jsonl"
OUTPUT_FILE = BASE_DIR / "main.html"
TZ = timezone(timedelta(hours=8))

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".ogg", ".mov", ".m4v", ".avi", ".mkv"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"}


def resolve_data_path(raw_path):
    """Resolve paths stored as either data/messages/... or messages/..."""
    if not raw_path:
        return None

    normalized = str(raw_path).replace("\\", "/")
    candidate = Path(normalized)
    if candidate.is_absolute():
        return candidate

    direct = BASE_DIR / normalized
    if direct.exists():
        return direct

    if normalized.startswith("data/"):
        stripped = BASE_DIR / normalized.removeprefix("data/")
        if stripped.exists():
            return stripped
        return stripped

    return direct


def html_relative_path(raw_path):
    path = resolve_data_path(raw_path)
    if path and path.exists():
        rel = path.relative_to(BASE_DIR).as_posix()
        return escape(rel, quote=True)

    normalized = str(raw_path or "").replace("\\", "/")
    if normalized.startswith("data/"):
        normalized = normalized[5:]
    return escape(normalized, quote=True)


def read_json(path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def sender_name(sender):
    if not isinstance(sender, dict):
        return "未知用户"
    return sender.get("card") or sender.get("nickname") or str(sender.get("user_id") or "未知用户")


def initials(name):
    name = str(name or "?").strip()
    if not name:
        return "?"
    return escape(name[:2])


def sender_name(sender):
    if not isinstance(sender, dict):
        return "未知用户"

    card = str(sender.get("card") or "").strip()
    nickname = str(sender.get("nickname") or "").strip()
    if card and nickname:
        return f"{card}({nickname})"
    if nickname:
        return nickname
    if card:
        return card
    return str(sender.get("user_id") or "未知用户")


def format_time(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def format_day(ts):
    if not ts:
        return "未知日期"
    try:
        return datetime.fromtimestamp(int(ts), TZ).strftime("%Y年%m月%d日")
    except (TypeError, ValueError, OSError):
        return "未知日期"


def media_lookup(media_files):
    lookup = {}
    for item in media_files or []:
        if not isinstance(item, dict):
            continue
        idx = item.get("segment_index")
        if idx is not None:
            lookup[int(idx)] = item
    return lookup


def render_text_with_links(text):
    escaped = escape(str(text or ""))
    url_pattern = re.compile(r"(https?://[^\s<]+)")
    return url_pattern.sub(r'<a href="\1" target="_blank" rel="noopener noreferrer">\1</a>', escaped)


def decode_base64_text(value):
    if not value:
        return ""
    try:
        raw = str(value).strip()
        raw += "=" * ((4 - len(raw) % 4) % 4)
        return unescape(base64.b64decode(raw).decode("utf-8", errors="replace"))
    except Exception:
        return ""


def parse_json_segment(data):
    raw = data.get("data") if isinstance(data, dict) else None
    if not raw:
        return None
    try:
        return json.loads(unescape(str(raw)))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def render_group_announcement(payload):
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    announce = meta.get("mannounce") if isinstance(meta.get("mannounce"), dict) else {}
    if not announce:
        return ""

    title = decode_base64_text(announce.get("title")) or decode_base64_text(payload.get("title")) or "群公告"
    body = decode_base64_text(announce.get("text")) or unescape(str(payload.get("prompt") or ""))
    publisher = announce.get("uin") or payload.get("uin") or ""
    fid = announce.get("fid") or ""

    if not body.strip():
        return ""

    publisher_html = f'<span>发布者 QQ {escape(str(publisher))}</span>' if publisher else ""
    fid_html = f'<span>公告ID {escape(str(fid))}</span>' if fid else ""

    return f'''
    <section class="announcement-card">
      <div class="announcement-kicker">群公告</div>
      <h2>{escape(title)}</h2>
      <div class="announcement-body">{render_text_with_links(body)}</div>
      <div class="announcement-meta">
        {publisher_html}
        {fid_html}
      </div>
    </section>
    '''


def render_local_or_remote_media(seg_type, data, media_item):
    src = ""
    label = "媒体"
    if media_item and media_item.get("path"):
        src = html_relative_path(media_item["path"])
        label = media_item.get("type") or seg_type or label
    elif isinstance(data, dict) and data.get("url"):
        src = escape(str(data["url"]), quote=True)
        label = seg_type or label

    if not src:
        summary = data.get("summary") if isinstance(data, dict) else ""
        return f'<span class="placeholder">[{escape(summary or seg_type or "媒体")}]</span>'

    suffix = Path(src.split("?", 1)[0]).suffix.lower()
    is_video = seg_type == "video" or suffix in VIDEO_EXTENSIONS
    is_audio = seg_type in {"record", "audio"} or suffix in AUDIO_EXTENSIONS
    is_image = seg_type == "image" or suffix in IMAGE_EXTENSIONS

    if is_video:
        return f'<video class="chat-video" controls preload="metadata" src="{src}">[视频]</video>'
    if is_audio:
        return f'<audio class="chat-audio" controls preload="metadata" src="{src}">[音频]</audio>'
    if is_image:
        return f'<a class="media-link" href="{src}" target="_blank"><img class="chat-image" src="{src}" alt="{escape(label)}" loading="lazy"></a>'
    return f'<a class="file-chip" href="{src}" target="_blank">打开{escape(label)}</a>'


def render_segment(segment, index, media_by_segment):
    if not isinstance(segment, dict):
        return ""

    seg_type = segment.get("type", "unknown")
    data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
    local_media = media_by_segment.get(index)

    if seg_type == "text":
        return f'<span>{render_text_with_links(data.get("text", ""))}</span>'
    if seg_type == "at":
        qq = data.get("qq") or data.get("user_id") or "unknown"
        return f'<span class="at">@{escape(str(qq))}</span>'
    if seg_type == "face":
        raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
        label = raw.get("faceText") or data.get("id") or "表情"
        return f'<span class="face">{escape(str(label))}</span>'
    if seg_type in {"image", "video", "record", "audio"}:
        return render_local_or_remote_media(seg_type, data, local_media)
    if seg_type == "reply":
        rid = data.get("id") or data.get("message_id") or ""
        return f'<span class="reply">回复 #{escape(str(rid))}</span>'
    if seg_type == "file":
        name = data.get("name") or data.get("file") or "文件"
        return f'<span class="file-chip">[文件] {escape(str(name))}</span>'
    if seg_type == "json":
        payload = parse_json_segment(data)
        if isinstance(payload, dict) and payload.get("app") == "com.tencent.mannounce":
            return render_group_announcement(payload)
        return ""

    return ""


def render_message_content(message):
    event = message.get("event") if isinstance(message.get("event"), dict) else {}
    segments = event.get("message")
    media_by_segment = media_lookup(message.get("media_files"))

    if isinstance(segments, list):
        rendered = [render_segment(seg, idx, media_by_segment) for idx, seg in enumerate(segments)]
        content = "".join(part for part in rendered if part)
    else:
        content = render_text_with_links(event.get("raw_message") or "")

    if not content.strip() and event.get("raw_message"):
        content = render_text_with_links(event.get("raw_message"))

    extra_media = []
    used = set(media_by_segment)
    for item in message.get("media_files") or []:
        idx = item.get("segment_index") if isinstance(item, dict) else None
        if idx in used:
            continue
        extra_media.append(render_local_or_remote_media(item.get("type", "media"), item.get("data", {}), item))

    if extra_media:
        content += "".join(extra_media)

    if not content.strip():
        content = '<span class="placeholder">[空消息]</span>'

    if message.get("recalled"):
        content = f'<div class="recall-note">此消息已撤回</div><div class="recalled-content">{content}</div>'

    return content


def load_message_records():
    index = read_json(INDEX_FILE)
    records = []

    for order, (message_id, raw_path) in enumerate(index.items(), start=1):
        path = resolve_data_path(raw_path)
        if not path or not path.exists():
            records.append(
                {
                    "kind": "missing",
                    "id": str(message_id),
                    "path": str(raw_path),
                    "order": order,
                    "time": None,
                }
            )
            continue

        try:
            payload = read_json(path)
        except Exception as exc:
            records.append(
                {
                    "kind": "missing",
                    "id": str(message_id),
                    "path": str(raw_path),
                    "order": order,
                    "time": None,
                    "error": str(exc),
                }
            )
            continue

        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        records.append(
            {
                "kind": "message",
                "id": str(message_id),
                "path": path,
                "order": order,
                "payload": payload,
                "time": event.get("time"),
                "sender_id": str(sender.get("user_id") or event.get("user_id") or "unknown"),
                "sender_name": sender_name(sender),
                "role": sender.get("role") or "",
                "group_name": event.get("group_name") or "聊天记录",
                "content": render_message_content(payload),
                "recalled": bool(payload.get("recalled")),
                "media_count": len(payload.get("media_files") or []),
            }
        )

    return records


def load_membership_events():
    records = []
    if not MEMBERSHIP_EVENTS_FILE.exists():
        return records

    with MEMBERSHIP_EVENTS_FILE.open("r", encoding="utf-8") as f:
        for order, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                records.append(
                    {
                        "kind": "membership_error",
                        "id": f"membership-error-{order}",
                        "order": order,
                        "time": None,
                        "error": str(exc),
                        "raw": line,
                    }
                )
                continue

            records.append(
                {
                    "kind": "membership",
                    "id": f"membership-{order}",
                    "order": order,
                    "time": event.get("event_time") or event.get("time"),
                    "event": event,
                }
            )

    return records


def record_sort_key(record):
    ts = record.get("time")
    if ts is None:
        return (1, record.get("order", 0), 2)
    priority = {"membership": 0, "message": 1, "missing": 2, "membership_error": 3}.get(record.get("kind"), 4)
    return (0, int(ts), priority, record.get("order", 0))


def membership_action_text(event):
    notice_type = event.get("notice_type")
    sub_type = event.get("sub_type") or ""
    if notice_type == "group_increase":
        if sub_type == "invite":
            return "加入群聊"
        if sub_type == "approve":
            return "加入群聊"
        return "加入群聊"
    if notice_type == "group_decrease":
        if sub_type == "kick":
            return "被移出群聊"
        return "退出群聊"
    return notice_type or "群成员事件"


def operator_text(event):
    operator_id = event.get("operator_id")
    if operator_id in (None, "", 0, "0"):
        return ""
    sub_type = event.get("sub_type")
    if event.get("notice_type") == "group_increase" and sub_type == "invite":
        verb = "邀请者"
    elif event.get("notice_type") == "group_decrease" and sub_type == "kick":
        verb = "操作者"
    else:
        verb = "批准者"
    return f'<span class="membership-operator">{verb} QQ {escape(str(operator_id))}</span>'


def render_membership_event(record):
    event = record["event"]
    user_id = event.get("user_id") or "未知QQ"
    time_text = format_time(record.get("time"))
    action = membership_action_text(event)
    operator = operator_text(event)
    sub_type = event.get("sub_type") or "unknown"
    notice_type = event.get("notice_type") or "unknown"
    detail = f'<span class="membership-detail">{escape(notice_type)} / {escape(sub_type)}</span>'
    return f'''
    <article class="membership-row" id="{escape(record["id"])}">
      <div class="membership-icon">群</div>
      <div class="membership-card">
        <strong>QQ {escape(str(user_id))}</strong>
        <span>{escape(action)}</span>
        {operator}
        <time>{escape(time_text)}</time>
        {detail}
      </div>
    </article>
    '''


def render_missing_record(record):
    return f'''
    <article class="system-row" id="msg-{escape(record["id"])}">
      <span>索引 #{record["order"]} 指向的消息文件缺失：{escape(record["path"])}</span>
    </article>
    '''


def render_membership_error(record):
    return f'''
    <article class="system-row" id="{escape(record["id"])}">
      <span>membership_events.jsonl 第 {record["order"]} 行解析失败：{escape(record["error"])}</span>
    </article>
    '''


def render_message_record(record, sender_rank):
    rank = sender_rank.get(record["sender_id"], 0)
    side = "other"
    hue = (rank * 53 + 205) % 360
    time_text = format_time(record.get("time"))
    role = f'<span class="role">{escape(record["role"])}</span>' if record.get("role") else ""

    return f'''
    <article class="message-row {side}" id="msg-{escape(record["id"])}" style="--avatar-hue:{hue}">
      <div class="avatar" title="{escape(record["sender_name"], quote=True)}">{initials(record["sender_name"])}</div>
      <div class="message-block">
        <div class="meta">
          <span class="name">{escape(record["sender_name"])}</span>
          {role}
          <time>{escape(time_text)}</time>
          <a class="msg-id" href="#msg-{escape(record["id"])}">#{escape(record["id"])}</a>
        </div>
        <div class="bubble">{record["content"]}</div>
      </div>
    </article>
    '''


def build_html(records):
    messages = [r for r in records if r.get("kind") == "message"]
    missing = [r for r in records if r.get("kind") == "missing"]
    membership_events = [r for r in records if r.get("kind") == "membership"]
    sender_counts = Counter(r["sender_id"] for r in messages)
    sender_rank = {sender_id: idx for idx, (sender_id, _) in enumerate(sender_counts.most_common())}
    group_name = messages[0]["group_name"] if messages else "聊天记录"
    total_media = sum(r.get("media_count", 0) for r in messages)
    recalled_count = sum(1 for r in messages if r.get("recalled"))

    rows = []
    current_day = None
    for record in records:
        day = format_day(record.get("time"))
        if record.get("time") is not None and day != current_day:
            current_day = day
            rows.append(f'<div class="day-divider"><span>{escape(day)}</span></div>')

        if record.get("kind") == "message":
            rows.append(render_message_record(record, sender_rank))
        elif record.get("kind") == "membership":
            rows.append(render_membership_event(record))
        elif record.get("kind") == "membership_error":
            rows.append(render_membership_error(record))
        else:
            rows.append(render_missing_record(record))

    return f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(group_name)} - 聊天记录</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f7fb;
      --panel: #ffffff;
      --ink: #1e293b;
      --muted: #64748b;
      --line: #dbe3ee;
      --accent: #0f766e;
      --bubble-other: #ffffff;
      --bubble-self: #dcfce7;
      --membership: #fef3c7;
      --shadow: 0 20px 50px rgba(15, 23, 42, .10);
    }}

    * {{ box-sizing: border-box; }}

    body {{
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at top left, rgba(20, 184, 166, .16), transparent 32rem),
        linear-gradient(180deg, #eef6ff 0%, var(--bg) 30%, #f8fafc 100%);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      line-height: 1.65;
    }}

    .app {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 28px 18px 48px;
    }}

    header {{
      position: sticky;
      top: 0;
      z-index: 5;
      margin: -1px -18px 22px;
      padding: 18px;
      background: rgba(244, 247, 251, .82);
      backdrop-filter: blur(18px);
      border-bottom: 1px solid rgba(219, 227, 238, .76);
    }}

    .header-inner {{
      max-width: 1120px;
      margin: 0 auto;
      display: grid;
      gap: 14px;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: end;
    }}

    h1 {{
      margin: 0;
      font-size: clamp(1.35rem, 2.2vw, 2rem);
      letter-spacing: 0;
      line-height: 1.25;
    }}

    .subtitle {{
      color: var(--muted);
      font-size: .92rem;
      margin-top: 4px;
    }}

    .stats {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}

    .stat {{
      min-width: 92px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, .78);
      border-radius: 8px;
      padding: 8px 10px;
      text-align: right;
      box-shadow: 0 8px 22px rgba(15, 23, 42, .06);
    }}

    .stat strong {{
      display: block;
      font-size: 1.15rem;
      line-height: 1.1;
    }}

    .stat span {{
      color: var(--muted);
      font-size: .78rem;
    }}

    main {{
      background: rgba(255, 255, 255, .72);
      border: 1px solid rgba(219, 227, 238, .9);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 22px;
    }}

    .day-divider {{
      display: flex;
      justify-content: center;
      margin: 20px 0;
    }}

    .day-divider span, .system-row {{
      color: var(--muted);
      background: #e8eef6;
      border: 1px solid #d6e0ec;
      border-radius: 999px;
      padding: 5px 12px;
      font-size: .84rem;
    }}

    .system-row {{
      display: block;
      width: fit-content;
      max-width: 100%;
      margin: 12px auto;
      border-radius: 8px;
    }}

    .membership-row {{
      display: flex;
      justify-content: center;
      align-items: center;
      gap: 8px;
      margin: 12px auto;
      color: #713f12;
    }}

    .membership-icon {{
      width: 30px;
      height: 30px;
      display: grid;
      place-items: center;
      border-radius: 999px;
      background: #fbbf24;
      color: #422006;
      font-weight: 700;
      font-size: .78rem;
      box-shadow: 0 8px 18px rgba(180, 83, 9, .16);
    }}

    .membership-card {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: center;
      gap: 6px;
      max-width: 100%;
      padding: 7px 12px;
      border-radius: 8px;
      border: 1px solid #fde68a;
      background: var(--membership);
      box-shadow: 0 8px 20px rgba(120, 53, 15, .08);
      font-size: .9rem;
    }}

    .membership-card time,
    .membership-detail,
    .membership-operator {{
      color: #92400e;
      font-size: .82rem;
    }}

    .membership-detail {{
      border: 1px solid #fcd34d;
      border-radius: 999px;
      padding: 0 7px;
      background: rgba(255, 251, 235, .65);
    }}

    .message-row {{
      display: grid;
      grid-template-columns: 42px minmax(0, 1fr);
      gap: 10px;
      margin: 14px 0;
      align-items: start;
    }}

    .message-row.self {{
      grid-template-columns: minmax(0, 1fr) 42px;
    }}

    .message-row.self .avatar {{
      grid-column: 2;
    }}

    .message-row.self .message-block {{
      grid-column: 1;
      grid-row: 1;
      align-items: flex-end;
    }}

    .avatar {{
      width: 42px;
      height: 42px;
      border-radius: 8px;
      display: grid;
      place-items: center;
      color: white;
      font-weight: 700;
      background: linear-gradient(135deg, hsl(var(--avatar-hue) 72% 42%), hsl(calc(var(--avatar-hue) + 35) 76% 56%));
      box-shadow: 0 10px 20px rgba(15, 23, 42, .16);
      overflow: hidden;
    }}

    .message-block {{
      display: flex;
      flex-direction: column;
      min-width: 0;
      max-width: min(760px, 100%);
    }}

    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
      color: var(--muted);
      font-size: .8rem;
      margin: 0 0 4px;
    }}

    .self .meta {{
      justify-content: flex-end;
    }}

    .name {{
      color: #334155;
      font-weight: 650;
    }}

    .role, .msg-id {{
      color: var(--muted);
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, .75);
      border-radius: 999px;
      padding: 0 7px;
      text-decoration: none;
    }}

    .bubble {{
      width: fit-content;
      max-width: 100%;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--bubble-other);
      box-shadow: 0 8px 22px rgba(15, 23, 42, .07);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}

    .self .bubble {{
      background: var(--bubble-self);
      border-color: #bce9ca;
    }}

    .bubble a {{
      color: var(--accent);
      text-decoration-thickness: 1px;
      text-underline-offset: 3px;
    }}

    .announcement-card {{
      min-width: min(420px, 100%);
      max-width: min(620px, 100%);
      padding: 12px;
      border-radius: 8px;
      border: 1px solid #bfdbfe;
      background: linear-gradient(180deg, #eff6ff 0%, #ffffff 100%);
      white-space: normal;
    }}

    .announcement-kicker {{
      display: inline-flex;
      width: fit-content;
      padding: 1px 8px;
      border-radius: 999px;
      background: #dbeafe;
      color: #1d4ed8;
      border: 1px solid #bfdbfe;
      font-size: .78rem;
      font-weight: 700;
    }}

    .announcement-card h2 {{
      margin: 8px 0 8px;
      font-size: 1.05rem;
      line-height: 1.35;
      letter-spacing: 0;
    }}

    .announcement-body {{
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}

    .announcement-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 10px;
      color: var(--muted);
      font-size: .8rem;
    }}

    .at, .face, .reply, .placeholder, .file-chip {{
      display: inline-flex;
      align-items: center;
      margin: 0 3px;
      min-height: 24px;
      border-radius: 999px;
      padding: 1px 8px;
      background: #eef6ff;
      color: #2563eb;
      border: 1px solid #cfe0ff;
      font-size: .9em;
      vertical-align: baseline;
      white-space: normal;
    }}

    .face {{
      background: #fff7ed;
      color: #c2410c;
      border-color: #fed7aa;
    }}

    .placeholder {{
      background: #f1f5f9;
      color: #64748b;
      border-color: #e2e8f0;
    }}

    .file-chip {{
      background: #f0fdfa;
      color: #0f766e;
      border-color: #99f6e4;
      text-decoration: none;
    }}

    .recall-note {{
      margin-bottom: 6px;
      color: #b45309;
      font-size: .86rem;
      font-weight: 650;
    }}

    .recalled-content {{
      opacity: .68;
      filter: grayscale(.18);
    }}

    .media-link {{
      display: block;
      width: fit-content;
      max-width: 100%;
      margin-top: 6px;
    }}

    .chat-image, .chat-video {{
      display: block;
      max-width: min(520px, 100%);
      max-height: 520px;
      border-radius: 8px;
      border: 1px solid rgba(148, 163, 184, .45);
      background: #f8fafc;
      object-fit: contain;
    }}

    .chat-video, .chat-audio {{
      margin-top: 6px;
      width: min(520px, 100%);
    }}

    footer {{
      text-align: center;
      color: var(--muted);
      font-size: .84rem;
      margin-top: 18px;
    }}

    @media (max-width: 720px) {{
      .app {{ padding: 16px 10px 32px; }}
      header {{ margin: -1px -10px 14px; padding: 14px 10px; }}
      .header-inner {{ grid-template-columns: 1fr; align-items: start; }}
      .stats {{ justify-content: flex-start; }}
      main {{ padding: 14px 10px; }}
      .message-row, .message-row.self {{
        grid-template-columns: 34px minmax(0, 1fr);
        gap: 8px;
      }}
      .message-row.self .avatar {{
        grid-column: 1;
      }}
      .message-row.self .message-block {{
        grid-column: 2;
      }}
      .self .meta {{
        justify-content: flex-start;
      }}
      .avatar {{
        width: 34px;
        height: 34px;
        font-size: .82rem;
      }}
      .bubble {{
        padding: 9px 10px;
      }}
      .membership-row {{
        align-items: flex-start;
      }}
      .membership-card {{
        justify-content: flex-start;
      }}
    }}
  </style>
</head>
<body>
  <div class="app">
    <header>
      <div class="header-inner">
        <div>
          <h1>{escape(group_name)}</h1>
          <div class="subtitle">按时间线复原的聊天记录，包含消息、加群与退群事件</div>
        </div>
        <div class="stats" aria-label="聊天统计">
          <div class="stat"><strong>{len(messages)}</strong><span>已读取消息</span></div>
          <div class="stat"><strong>{len(missing)}</strong><span>缺失索引</span></div>
          <div class="stat"><strong>{len(membership_events)}</strong><span>成员事件</span></div>
          <div class="stat"><strong>{total_media}</strong><span>本地媒体</span></div>
          <div class="stat"><strong>{recalled_count}</strong><span>撤回消息</span></div>
        </div>
      </div>
    </header>
    <main>
      {''.join(rows)}
    </main>
    <footer>由 generate.py 生成，共处理 {len(records)} 条时间线记录。</footer>
  </div>
</body>
</html>'''


def load_records():
    records = load_message_records() + load_membership_events()
    return sorted(records, key=record_sort_key)


def main():
    records = load_records()
    html = build_html(records)
    OUTPUT_FILE.write_text(html, encoding="utf-8")

    messages = sum(1 for r in records if r.get("kind") == "message")
    missing = sum(1 for r in records if r.get("kind") == "missing")
    membership = sum(1 for r in records if r.get("kind") == "membership")
    print(f"Generated {OUTPUT_FILE}")
    print(f"Messages rendered: {messages}; missing index entries: {missing}; membership events: {membership}")


if __name__ == "__main__":
    main()
