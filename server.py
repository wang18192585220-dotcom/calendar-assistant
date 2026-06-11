#!/usr/bin/env python3
"""Bridge server: AI analysis + Google Calendar integration."""

import json
import subprocess
import sys
import os
import time
import hashlib
import urllib.request
import tempfile
import re
import cgi
import sqlite3
import uuid
from datetime import datetime, timedelta
from io import BytesIO
from http.server import HTTPServer, BaseHTTPRequestHandler

import user_profile

HERMES_HOME = "/Users/wangshiyu/.hermes/profiles/energy-management"
GAPI = os.path.join(HERMES_HOME, "skills/productivity/google-workspace/scripts/google_api.py")
HERMES_PYTHON = "/Users/wangshiyu/.hermes/hermes-agent/venv/bin/python"

# Read DeepSeek API key from .env
ENV_PATH = os.path.join(HERMES_HOME, ".env")
DEEPSEEK_API_KEY = ""
try:
    with open(ENV_PATH) as f:
        for line in f:
            if line.startswith("DEEPSEEK_API_KEY="):
                DEEPSEEK_API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
except FileNotFoundError:
    pass

PORT = 8765

# ==================== Task History Database ====================
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_history.db")

def _init_db():
    """Initialize SQLite database with tasks and events tables."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT,
            priority TEXT,
            project_name TEXT,
            scheduled_start TEXT,
            scheduled_end TEXT,
            estimated_duration_minutes INTEGER,
            actual_duration_minutes INTEGER,
            status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT REFERENCES tasks(id),
            event_type TEXT NOT NULL,
            payload TEXT,
            created_at TEXT NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON task_events(event_type)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_created ON task_events(created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
    conn.commit()
    conn.close()

_init_db()

def _db_conn():
    """Get a database connection with row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# In-memory cache for repeated queries (task text → analysis result)
import hashlib
_ANALYSIS_CACHE = {}  # {text_hash: (analysis_dict, timestamp)}

ANALYZE_PROMPT = """你是一个智能日程规划助手，服务于一位大学生（王世宇）。你不仅解析任务，还会根据任务类型智能估算耗时并建议开始时间。

用户背景：
- 大学生，有时区 UTC+8
- 常见任务类型及典型耗时参考：
  - MKT62704 课程作业（Part A/B/C）：2-4小时/部分
  - 论文写作：3-6小时
  - TikTok 达人筛选/运营：1-2小时
  - 课程预习/复习：1-2小时
  - 考试复习：2-4小时
  - 阅读文献/论文：1-2小时
  - 小组讨论/会议：0.5-1小时
  - 内容创作（小红书/TikTok）：1-2小时

必须严格按照以下JSON格式返回（只返回JSON，不要任何前缀、后缀或markdown标记）：
{"title":"精简标题","priority":"P1","priorityLabel":"紧急且重要","category":"学习","projectName":"建议项目名","dateISO":"2026-06-15","dateStr":"明天","startTime":"15:00","endTime":"17:00","durationMinutes":120,"durationStr":"2小时","location":"图书馆","reason":"分析理由","aiEstimatedDuration":120,"aiDurationReason":"该类型任务通常需要2小时","aiSuggestedStart":"建议周三下午开始，周四有课冲突"}

新增字段说明（智能估算）：
- aiEstimatedDuration: 根据任务类型和用户习惯估算的耗时（分钟），即使原文提到了时间也给出你的独立判断
- aiDurationReason: 简短说明为什么估算这个时长（如"MKT作业通常需要3小时"）
- aiSuggestedStart: 如果原文没有明确时间，给出一句话建议（如"建议今天下午开始，截止日期是周五"）

规则：
- 无法确定的字段填 null（不是字符串"null"）
- 时间用24小时制 HH:MM
- 中文数字时间要转换："九点半"→"09:30","下午三点"→"15:00"
- 无上下文的"X点到Y点"默认下午（13:00-23:00）
- "半小时"=30分钟，"两小时"=120分钟
- "明天/后天/下周X"需推算具体日期填入dateISO
- priority只返回P1/P2/P3/P4
- category只返回：学习/工作/项目/生活/内容创作/会议/提醒
- aiEstimatedDuration 必须是合理的数值，参考上面的典型耗时"""

SCHEDULE_PROMPT = """你是一个智能排班助手。根据用户已有的日历事件和待安排的任务，为每个任务建议最佳时间。

当前已有的日历事件（这些时间段已被占用）：
{calendar_events}

待安排的任务列表（需排入空闲时段）：
{tasks_text}

用户背景：大学生，时区 UTC+8。偏好早上9点后开始，晚上10点前结束。喜欢连续工作不碎片化。

请为每个任务推荐一个最佳时间段。严格按以下JSON数组格式返回（只返回JSON数组）：
[
  {{"taskIndex":0,"title":"任务标题","suggestedDate":"2026-06-15","suggestedStart":"14:00","suggestedEnd":"16:30","durationMinutes":150,"reason":"周三下午空闲，放在周二作业之后"}},
  ...
]

排班规则：
- 优先安排 P1（紧急且重要）的任务
- 同类型任务尽量连续安排（如学习类任务集中处理）
- 避免在已有课程/会议前后安排高强度任务
- 每个任务之间留30分钟缓冲
- 如果一天排满了就推到下一天
- 一个任务块不超过3小时，超过则拆分为多天
- dateISO 必须是未来7天内的日期（今天是 {today}）
- reason 字段简要说明为何选择这个时间段"""

BATCH_EXTRACT_PROMPT = """你是一个日程规划助手。从以下文档/图片中提取所有独立的任务/事件/课程。

文档内容：
{text}

请将每个独立的任务/事件/课程拆解出来，以JSON数组格式返回（只返回JSON数组，不要任何前缀、后缀或markdown标记）：
[
  {{"title":"精简标题","dateISO":"2026-06-19","dateStr":"周四","startTime":"09:00","endTime":"10:30","durationMinutes":90,"location":"教室A","category":"学习","projectName":"课程名称","reason":"周一第1节课"}},
  {{"title":"另一个任务","dateISO":"2026-06-19","dateStr":"周四","startTime":"14:00","endTime":"16:00","durationMinutes":120,"location":null,"category":"工作"}}
]

规则：
- 每个任务必须独立一行
- 今天日期是 {today}，根据文档中的星期/日期推算出 dateISO
- 如果只有星期没有具体日期（如"周四"），推算最近的下一个该星期几
- 时间用24小时制 HH:MM
- 无法确定的字段填 null
- category只能是：学习/工作/项目/生活/内容创作/会议/提醒
- 从课表中提取的课程 category 应为"学习"
- 不要漏掉任何任务"""



class BridgeHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        """Handle GET requests (calendar read, profile, stats)."""
        if self.path.startswith("/api/calendar/read"):
            self._handle_calendar_read()
        elif self.path.startswith("/api/profile"):
            self._handle_profile()
        elif self.path.startswith("/api/history/stats"):
            self._handle_history_stats()
        else:
            self.send_error(404, "Not found")

    def do_POST(self):
        content_type = self.headers.get("Content-Type", "")

        # Handle multipart file upload
        if content_type.startswith("multipart/form-data"):
            self._handle_upload()
            return

        # Handle JSON requests (existing)
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._json_response(400, {"status": "error", "message": f"Invalid JSON: {e}"})
            return

        if self.path == "/api/calendar":
            self._handle_calendar(data)
        elif self.path == "/api/analyze":
            self._handle_analyze(data)
        elif self.path == "/api/schedule":
            self._handle_schedule(data)
        elif self.path == "/api/history/log":
            self._handle_history_log(data)
        else:
            self.send_error(404, "Not found")

    def _handle_analyze(self, data):
        text = data.get("text", "").strip()
        if not text:
            self._json_response(400, {"status": "error", "message": "Empty text"})
            return

        # Check cache first
        cache_key = hashlib.md5(text.encode()).hexdigest()
        if cache_key in _ANALYSIS_CACHE:
            cached_result, ts = _ANALYSIS_CACHE[cache_key]
            if time.time() - ts < 300:  # 5 min cache
                print(f"[ANALYZE] Cache hit for: {text[:50]}", flush=True)
                # Mark as cached
                resp = dict(cached_result)
                resp["source"] = "cached"
                self._json_response(200, resp)
                return

        if not DEEPSEEK_API_KEY:
            result = self._keyword_analyze(text)
            self._json_response(200, result)
            return

        try:
            req = urllib.request.Request(
                "https://api.deepseek.com/v1/chat/completions",
                data=json.dumps({
                    "model": "deepseek-chat",  # v4-flash: ~1s vs v4-pro: ~3s
                    "messages": [
                        {"role": "system", "content": self._build_analyze_prompt()},
                        {"role": "user", "content": f"分析这个任务：{text}"}
                    ],
                    "temperature": 0.1,      # lower = more deterministic
                    "max_tokens": 300
                }).encode(),
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json"
                }
            )

            print(f"[ANALYZE] Calling DeepSeek for: {text[:80]}", flush=True)
            t0 = time.time()
            resp = urllib.request.urlopen(req, timeout=10)  # shorter timeout
            result = json.loads(resp.read())
            content = result["choices"][0]["message"]["content"].strip()
            elapsed = time.time() - t0
            print(f"[ANALYZE] API took {elapsed:.1f}s", flush=True)

            # Robust JSON extraction
            analysis = self._extract_json(content)

            if analysis:
                response_data = {"status": "ok", "analysis": analysis}
                _ANALYSIS_CACHE[cache_key] = (response_data, time.time())
                self._json_response(200, response_data)
            else:
                print(f"[ANALYZE] JSON extraction failed, raw: {content[:200]}", flush=True)
                result = self._keyword_analyze(text)
                self._json_response(200, result)

        except Exception as e:
            print(f"[ANALYZE] Error: {e}", flush=True)
            result = self._keyword_analyze(text)
            self._json_response(200, result)

    def _extract_json(self, content):
        """Robust JSON extraction from LLM response — handles markdown, extra text, etc."""
        import re
        # Strategy 1: Remove ```json ... ``` blocks
        md_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', content, re.DOTALL)
        if md_match:
            content = md_match.group(1).strip()

        # Strategy 2: Find the first { and matching }
        start = content.find('{')
        if start == -1:
            return None
        # Find matching brace
        depth = 0
        end = -1
        for i in range(start, len(content)):
            if content[i] == '{':
                depth += 1
            elif content[i] == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            return None

        json_str = content[start:end+1]
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            # Strategy 3: Fix common issues
            # - Fix Python None → null
            json_str = json_str.replace(': None', ': null').replace(': True', ': true').replace(': False', ': false')
            # - Fix unquoted null
            json_str = re.sub(r':\s*null(?!\s*[,}\]])', ': null', json_str)
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                pass
        return None

    def _keyword_analyze(self, text):
        """Keyword-based fallback analysis."""
        cat_keywords = {
            "学习": ["学习", "作业", "课程", "考试", "阅读", "复习", "论文", "课题", "笔记", "预习", "听课", "上课"],
            "工作": ["工作", "实习", "客户", "达人", "运营", "筛选", "面试", "简历", "求职", "报告", "汇报", "数据"],
            "项目": ["项目", "创业", "自媒体", "网站", "开发", "上线", "产品", "方案"],
            "生活": ["签证", "购物", "出行", "旅游", "搬家", "打扫", "做饭", "买菜", "账单", "房租"],
            "内容创作": ["小红书", "抖音", "TikTok", "视频", "脚本", "内容创作", "剪辑", "拍摄", "发布", "公众号"],
            "会议": ["会议", "开会", "沟通", "讨论", "面谈", "小组", "约谈", "同步"],
            "提醒": ["提醒", "记得", "别忘了", "备忘", "不要忘"],
        }
        matched_cat = ""
        max_score = 0
        for cat, keywords in cat_keywords.items():
            score = sum(1 for kw in keywords if kw in text)
            if score > max_score:
                max_score = score
                matched_cat = cat

        return {
            "status": "ok",
            "source": "keyword",
            "analysis": {
                "title": text[:40],
                "priority": "P2",
                "priorityLabel": "重要但不紧急",
                "category": matched_cat or "工作",
                "projectName": matched_cat + "相关任务" if matched_cat else "待分类",
                "reason": "关键词匹配（离线模式）"
            }
        }

    def _handle_calendar(self, data):
        summary = data.get("summary", "").strip()
        start_dt = data.get("start")
        end_dt = data.get("end")
        description = data.get("description", "")
        location = data.get("location", "")

        if not summary:
            self._json_response(400, {"status": "error", "message": "Missing: summary"})
            return
        if not start_dt or not end_dt:
            self._json_response(400, {"status": "error", "message": "Missing: start/end time"})
            return

        cmd = [HERMES_PYTHON, GAPI, "calendar", "create",
               "--summary", summary, "--start", start_dt, "--end", end_dt]
        if description:
            cmd += ["--description", description]
        if location:
            cmd += ["--location", location]

        env = os.environ.copy()
        env["HERMES_HOME"] = HERMES_HOME

        try:
            print(f"[CALENDAR] Creating: {summary[:50]} | {start_dt} → {end_dt}", flush=True)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
            output = result.stdout.strip()
            if result.returncode == 0:
                try:
                    resp = json.loads(output)
                    self._json_response(200, {
                        "status": "created",
                        "id": resp.get("id", ""),
                        "summary": resp.get("summary", summary),
                        "htmlLink": resp.get("htmlLink", "")
                    })
                except json.JSONDecodeError:
                    self._json_response(200, {"status": "created", "raw": output})
            else:
                err = result.stderr.strip() or output or "Unknown error"
                print(f"[CALENDAR] Error: {err[:300]}", flush=True)
                self._json_response(500, {"status": "error", "message": err})
        except Exception as e:
            self._json_response(500, {"status": "error", "message": str(e)})

    def _handle_upload(self):
        """Handle file upload: extract text, AI decompose into tasks."""
        content_type = self.headers.get("Content-Type", "")
        content_length = int(self.headers.get("Content-Length", 0))

        # Parse multipart form data
        boundary = content_type.split("boundary=")[1].strip()
        raw = self.rfile.read(content_length)

        # Extract file data from multipart
        file_data, filename = self._parse_multipart(raw, boundary)
        if not file_data:
            self._json_response(400, {"status": "error", "message": "No file found in upload"})
            return

        print(f"[UPLOAD] Received: {filename} ({len(file_data)} bytes)", flush=True)

        # Save to temp file
        suffix = os.path.splitext(filename)[1].lower() if filename else ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
            tf.write(file_data)
            tmp_path = tf.name

        try:
            # Extract text from file
            text = self._extract_text(tmp_path, suffix)
            if not text or not text.strip():
                self._json_response(200, {
                    "status": "ok",
                    "source": "ocr",
                    "raw_text": "",
                    "tasks": [],
                    "message": "无法从文件中识别出文字内容"
                })
                return

            print(f"[UPLOAD] Extracted {len(text)} chars of text", flush=True)

            # Send to AI for task decomposition
            if DEEPSEEK_API_KEY:
                tasks = self._batch_extract_tasks(text)
            else:
                tasks = []

            self._json_response(200, {
                "status": "ok",
                "source": "ai" if DEEPSEEK_API_KEY else "keyword",
                "raw_text": text[:5000],
                "total_chars": len(text),
                "tasks": tasks,
                "message": f"共识别 {len(text)} 字，提取 {len(tasks)} 个任务" if tasks else "未能从文档中识别出任务"
            })

        except Exception as e:
            print(f"[UPLOAD] Error: {e}", flush=True)
            self._json_response(500, {"status": "error", "message": str(e)})
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _parse_multipart(self, raw, boundary):
        """Parse multipart form data, return (file_bytes, filename)."""
        boundary_bytes = boundary.encode()
        parts = raw.split(b"--" + boundary_bytes)

        for part in parts:
            if b"Content-Disposition" not in part:
                continue

            # Extract filename
            header_end = part.find(b"\r\n\r\n")
            if header_end == -1:
                continue
            headers = part[:header_end].decode(errors="replace")
            file_match = re.search(r'filename="([^"]*)"', headers)
            if not file_match:
                continue

            filename = file_match.group(1)
            content = part[header_end + 4:]  # After \r\n\r\n
            # Remove trailing boundary
            content = content.rstrip(b"\r\n-")
            # Remove trailing \r\n
            content = content.rstrip(b"\r\n")

            return content, filename

        return None, None

    def _extract_text(self, filepath, suffix):
        """Extract text from PDF or image file."""
        suffix = suffix.lower()

        if suffix == ".pdf":
            # Use pdftotext
            result = subprocess.run(
                ["pdftotext", filepath, "-"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                return result.stdout.strip()
            print(f"[UPLOAD] pdftotext failed: {result.stderr[:200]}", flush=True)
            return ""

        elif suffix in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"):
            # Use tesseract OCR
            result = subprocess.run(
                ["tesseract", filepath, "stdout", "-l", "chi_sim+eng", "--psm", "3"],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                text = result.stdout.strip()
                # Also capture stderr (tesseract prints version info there)
                if not text and result.stderr:
                    text = result.stderr.strip()
                return text
            print(f"[UPLOAD] tesseract failed: {result.stderr[:200]}", flush=True)
            return ""

        else:
            return ""

    def _batch_extract_tasks(self, text):
        """Send extracted text to DeepSeek to decompose into individual tasks.
        For long documents, use chunking to avoid truncation."""
        today = time.strftime("%Y-%m-%d")

        # For short texts: single pass with full content
        if len(text) <= 8000:
            prompt = BATCH_EXTRACT_PROMPT.format(text=text, today=today)
            return self._call_deepseek_batch(prompt)

        # For long texts: chunk by sections, merge results
        print(f"[UPLOAD] Long doc ({len(text)} chars), chunking...", flush=True)
        all_tasks = []
        chunks = self._chunk_text(text, 6000)

        for i, chunk in enumerate(chunks):
            chunk_prompt = BATCH_EXTRACT_PROMPT.format(
                text=f"(第 {i+1}/{len(chunks)} 部分)\n\n{chunk}",
                today=today
            )
            tasks = self._call_deepseek_batch(chunk_prompt)
            all_tasks.extend(tasks)
            print(f"[UPLOAD] Chunk {i+1}/{len(chunks)}: {len(tasks)} tasks", flush=True)

        # Deduplicate by title similarity
        all_tasks = self._deduplicate_tasks(all_tasks)
        return all_tasks

    def _chunk_text(self, text, chunk_size):
        """Split text into chunks at natural boundaries (paragraphs, lines)."""
        chunks = []
        paragraphs = text.split('\n\n')
        current = ''
        for para in paragraphs:
            if len(current) + len(para) > chunk_size and current:
                chunks.append(current.strip())
                current = para
            else:
                current += ('\n\n' if current else '') + para
        if current.strip():
            chunks.append(current.strip())
        return chunks

    def _deduplicate_tasks(self, tasks):
        """Remove duplicate tasks based on title similarity."""
        seen = set()
        unique = []
        for t in tasks:
            key = (t.get('title', '')[:20] + '|' + 
                   str(t.get('startTime', '')) + '|' + 
                   str(t.get('dateISO', '')))
            if key not in seen:
                seen.add(key)
                unique.append(t)
        return unique

    def _call_deepseek_batch(self, prompt):
        """Single DeepSeek API call for batch extraction."""
        try:
            req = urllib.request.Request(
                "https://api.deepseek.com/v1/chat/completions",
                data=json.dumps({
                    "model": "deepseek-v4-pro",  # v4-pro: more accurate for document decomposition
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": "请提取所有任务"}
                    ],
                    "temperature": 0.1,
                    "max_tokens": 4000      # ↑ 2000 → 4000 for longer docs
                }).encode(),
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json"
                }
            )

            print(f"[UPLOAD] Calling DeepSeek (prompt {len(prompt)} chars)...", flush=True)
            t0 = time.time()
            resp = urllib.request.urlopen(req, timeout=45)  # ↑ 30 → 45s for long docs
            result = json.loads(resp.read())
            elapsed = time.time() - t0
            content = result["choices"][0]["message"]["content"].strip()
            print(f"[UPLOAD] AI took {elapsed:.1f}s, response {len(content)} chars", flush=True)

            tasks = self._extract_json_array(content)
            if not tasks:
                # Diagnostic: log raw response when extraction fails
                print(f"[UPLOAD] JSON extraction failed! Raw preview: {content[:300]}", flush=True)
            return tasks

        except Exception as e:
            print(f"[UPLOAD] AI error: {e}", flush=True)
            return []

    def _extract_json_array(self, content):
        """Robust JSON array extraction from LLM response."""
        # Strategy 1: Remove markdown blocks
        md_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', content, re.DOTALL)
        if md_match:
            content = md_match.group(1).strip()

        # Strategy 2: Find the first [ and matching ]
        start = content.find('[')
        if start == -1:
            return []
        depth = 0
        end = -1
        for i in range(start, len(content)):
            if content[i] == '[':
                depth += 1
            elif content[i] == ']':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            return []

        json_str = content[start:end+1]
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            # Try fixing common issues
            json_str = json_str.replace(': None', ': null').replace(': True', ': true').replace(': False', ': false')
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                pass
        return []

    # ==================== NEW: Calendar Read ====================
    def _handle_calendar_read(self):
        """GET /api/calendar/read?days=14 — read upcoming calendar events."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        days = int(params.get("days", [14])[0])

        events = self._read_calendar_events(days)
        free_slots = self._find_free_slots(events, days)

        self._json_response(200, {
            "status": "ok",
            "events": events,
            "freeSlots": free_slots,
            "totalEvents": len(events),
            "totalFreeSlots": len(free_slots)
        })

    def _read_calendar_events(self, days=14):
        """Call google_api.py to list calendar events."""
        from datetime import datetime, timedelta
        now = datetime.now()
        time_min = now.strftime("%Y-%m-%dT00:00:00+08:00")
        time_max = (now + timedelta(days=days)).strftime("%Y-%m-%dT23:59:59+08:00")

        cmd = [HERMES_PYTHON, GAPI, "calendar", "list",
               "--start", time_min, "--end", time_max, "--max", "100"]
        env = os.environ.copy()
        env["HERMES_HOME"] = HERMES_HOME

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
            if result.returncode == 0 and result.stdout.strip():
                events = json.loads(result.stdout)
                # Filter out cancelled events
                events = [e for e in events if e.get("status") != "cancelled"]
                print(f"[CALENDAR] Read {len(events)} events", flush=True)
                return events
            else:
                print(f"[CALENDAR] Read failed: {result.stderr[:200]}", flush=True)
        except Exception as e:
            print(f"[CALENDAR] Read error: {e}", flush=True)
        return []

    def _find_free_slots(self, events, days=7):
        """Find free time slots between calendar events (9:00-22:00)."""
        from datetime import datetime, timedelta
        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        free_slots = []

        for day_offset in range(days):
            day = today + timedelta(days=day_offset)
            day_str = day.strftime("%Y-%m-%d")

            # Get events for this day
            day_events = []
            for e in events:
                start_str = e.get("start", "")
                if start_str.startswith(day_str) or (
                   "T" in start_str and start_str[:10] == day_str):
                    day_events.append(e)

            if not day_events:
                # Entire day is free (9:00-22:00)
                free_slots.append({
                    "date": day_str,
                    "start": "09:00",
                    "end": "22:00",
                    "label": "全天空闲"
                })
                continue

            # Sort by start time
            day_events.sort(key=lambda e: e.get("start", ""))

            # Find gaps between events
            day_start = 9 * 60  # 9:00 in minutes
            day_end = 22 * 60   # 22:00 in minutes
            occupied = []

            for e in day_events:
                s = e.get("start", "")
                e_time = e.get("end", "")
                try:
                    if "T" in s:
                        sh, sm = int(s[11:13]), int(s[14:16])
                        eh, em = int(e_time[11:13]), int(e_time[14:16])
                        occupied.append((sh * 60 + sm, eh * 60 + em))
                except (ValueError, IndexError):
                    continue

            occupied.sort()

            current = day_start
            for (occ_start, occ_end) in occupied:
                if current < occ_start:
                    gap = occ_start - current
                    if gap >= 60:  # Only report gaps >= 1 hour
                        free_slots.append({
                            "date": day_str,
                            "start": f"{current // 60:02d}:{current % 60:02d}",
                            "end": f"{occ_start // 60:02d}:{occ_start % 60:02d}",
                            "durationMinutes": gap,
                            "label": f"空闲 {gap // 60}小时{gap % 60}分钟"
                        })
                current = max(current, occ_end)

            # Remaining time after last event
            if current < day_end:
                gap = day_end - current
                if gap >= 60:
                    free_slots.append({
                        "date": day_str,
                        "start": f"{current // 60:02d}:{current % 60:02d}",
                        "end": "22:00",
                        "durationMinutes": gap,
                        "label": f"空闲 {gap // 60}小时{gap % 60}分钟"
                    })

        return free_slots

    # ==================== NEW: Smart Schedule (Auto-排班) ====================
    def _handle_schedule(self, data):
        """POST /api/schedule — AI auto-scheduling for multiple tasks."""
        tasks = data.get("tasks", [])
        if not tasks:
            self._json_response(400, {"status": "error", "message": "No tasks provided"})
            return

        print(f"[SCHEDULE] Auto-scheduling {len(tasks)} tasks...", flush=True)

        # Read calendar events
        events = self._read_calendar_events(14)

        # Format calendar events for the prompt
        events_text = ""
        for e in events:
            summary = e.get("summary", "无标题")
            start = e.get("start", "")
            end = e.get("end", "")
            # Extract date + time for readability
            if "T" in start:
                date_part = start[:10]
                start_time = start[11:16]
                end_time = end[11:16] if "T" in end else "?"
                events_text += f"  {date_part} {start_time}-{end_time}: {summary}\n"
            else:
                events_text += f"  {start} (全天): {summary}\n"

        # Format tasks for the prompt
        tasks_text = ""
        for i, t in enumerate(tasks):
            title = t.get("title", f"任务{i+1}")
            priority = t.get("priority", "P3")
            category = t.get("category", "")
            deadline = t.get("deadline", "")
            tasks_text += f"  [{i}] {title} | 等级:{priority} | 分类:{category}"
            if deadline:
                tasks_text += f" | 截止:{deadline}"
            tasks_text += "\n"

        if not DEEPSEEK_API_KEY:
            self._json_response(200, {
                "status": "ok",
                "source": "keyword",
                "schedule": [],
                "message": "需要 DeepSeek API Key 才能智能排班"
            })
            return

        today = time.strftime("%Y-%m-%d")
        prompt = self._build_schedule_prompt(
            events_text or "（暂无日历事件）",
            tasks_text
        )

        try:
            req = urllib.request.Request(
                "https://api.deepseek.com/v1/chat/completions",
                data=json.dumps({
                    "model": "deepseek-chat",
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": "请为这些任务安排最佳时间"}
                    ],
                    "temperature": 0.3,
                    "max_tokens": 2000
                }).encode(),
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json"
                }
            )

            print(f"[SCHEDULE] Calling DeepSeek...", flush=True)
            t0 = time.time()
            resp = urllib.request.urlopen(req, timeout=30)
            result = json.loads(resp.read())
            content = result["choices"][0]["message"]["content"].strip()
            elapsed = time.time() - t0
            print(f"[SCHEDULE] AI took {elapsed:.1f}s", flush=True)

            schedule = self._extract_json_array(content)
            print(f"[SCHEDULE] AI suggested {len(schedule)} time slots", flush=True)

            self._json_response(200, {
                "status": "ok",
                "source": "ai",
                "schedule": schedule,
                "calendarEvents": events,
                "freeSlotsCount": len(self._find_free_slots(events))
            })

        except Exception as e:
            print(f"[SCHEDULE] Error: {e}", flush=True)
            self._json_response(500, {"status": "error", "message": str(e)})

    # ==================== History Logging (Task Historian) ====================
    def _handle_history_log(self, data):
        """POST /api/history/log — record a task lifecycle event."""
        event_type = data.get("event_type", "")
        if not event_type:
            self._json_response(400, {"status": "error", "message": "Missing: event_type"})
            return

        task_id = data.get("task_id", str(uuid.uuid4()))
        now = datetime.now().isoformat()

        conn = _db_conn()
        c = conn.cursor()

        # Handle different event types
        if event_type == "task_created":
            # Upsert the task record
            c.execute("""
                INSERT INTO tasks (id, title, category, priority, project_name,
                                   scheduled_start, scheduled_end,
                                   estimated_duration_minutes, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title, category=excluded.category,
                    priority=excluded.priority, project_name=excluded.project_name,
                    scheduled_start=excluded.scheduled_start,
                    scheduled_end=excluded.scheduled_end,
                    estimated_duration_minutes=excluded.estimated_duration_minutes
            """, (
                task_id,
                data.get("title", ""),
                data.get("category", ""),
                data.get("priority", ""),
                data.get("project_name", ""),
                data.get("scheduled_start", ""),
                data.get("scheduled_end", ""),
                data.get("estimated_duration_minutes"),
                now,
            ))
            print(f"[HISTORY] Task created: {data.get('title', task_id)[:50]}", flush=True)

        elif event_type == "task_completed":
            actual_duration = data.get("actual_duration_minutes")
            c.execute("""
                UPDATE tasks SET status='completed', completed_at=?,
                actual_duration_minutes=?
                WHERE id=?
            """, (now, actual_duration, task_id))
            print(f"[HISTORY] Task completed: {task_id}", flush=True)

        elif event_type == "task_rescheduled":
            c.execute("""
                UPDATE tasks SET scheduled_start=?, scheduled_end=?
                WHERE id=?
            """, (
                data.get("new_start", ""),
                data.get("new_end", ""),
                task_id,
            ))
            print(f"[HISTORY] Task rescheduled: {task_id}", flush=True)

        elif event_type == "task_cancelled":
            c.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task_id,))
            print(f"[HISTORY] Task cancelled: {task_id}", flush=True)

        elif event_type == "ai_correction":
            # User manually overrode AI suggestion
            print(f"[HISTORY] AI correction: {task_id}", flush=True)

        # Log the event
        c.execute("""
            INSERT INTO task_events (task_id, event_type, payload, created_at)
            VALUES (?, ?, ?, ?)
        """, (
            task_id,
            event_type,
            json.dumps(data.get("payload", {}), ensure_ascii=False),
            now,
        ))

        conn.commit()
        conn.close()

        self._json_response(200, {"status": "ok", "task_id": task_id})

    # ==================== User Profile ====================
    def _handle_profile(self):
        """GET /api/profile?days=30 — return user profile summary."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        days = int(params.get("days", [30])[0])

        try:
            summary = user_profile.build_profile_summary(DB_PATH, days)
            stats = user_profile.get_detailed_stats(DB_PATH, days)
            self._json_response(200, {
                "status": "ok",
                "summary": summary,
                "stats": stats,
            })
        except Exception as e:
            print(f"[PROFILE] Error: {e}", flush=True)
            self._json_response(500, {"status": "error", "message": str(e)})

    def _handle_history_stats(self):
        """GET /api/history/stats?days=30 — return detailed history stats."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        days = int(params.get("days", [30])[0])

        try:
            stats = user_profile.get_detailed_stats(DB_PATH, days)
            self._json_response(200, {"status": "ok", **stats})
        except Exception as e:
            print(f"[STATS] Error: {e}", flush=True)
            self._json_response(500, {"status": "error", "message": str(e)})

    # ==================== Profile-Injected Prompts ====================
    def _build_analyze_prompt(self):
        """Build ANALYZE_PROMPT with user profile injected."""
        profile_text = user_profile.build_profile_summary(DB_PATH, 30)
        return user_profile.inject_profile_into_prompt(ANALYZE_PROMPT, profile_text)

    def _build_schedule_prompt(self, calendar_events, tasks_text):
        """Build SCHEDULE_PROMPT with user profile injected."""
        today = time.strftime("%Y-%m-%d")
        base_prompt = SCHEDULE_PROMPT.format(
            calendar_events=calendar_events,
            tasks_text=tasks_text,
            today=today,
        )
        profile_text = user_profile.build_profile_summary(DB_PATH, 30)
        return user_profile.inject_profile_into_prompt(base_prompt, profile_text)

    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, format, *args):
        print(f"[{self.log_date_time_string()}] {args[0]}", flush=True)


def main():
    print(f"🚀 Bridge running on http://localhost:{PORT}")
    print(f"   GET  /api/calendar/read — Read calendar events + find free slots")
    print(f"   POST /api/analyze       — AI task analysis (smart duration estimate)")
    print(f"   POST /api/calendar      — Google Calendar write")
    print(f"   POST /api/schedule      — AI auto-scheduling (智能排班)")
    print(f"   POST /api/upload        — File upload + OCR + multi-task extraction")
    print(f"   POST /api/history/log   — Task lifecycle event logging 🆕")
    print(f"   GET  /api/profile       — User profile summary (学习画像) 🆕")
    print(f"   GET  /api/history/stats — Detailed task history stats 🆕")
    print(f"   DeepSeek API: {'✅ configured' if DEEPSEEK_API_KEY else '❌ not found (keyword fallback)'}")
    HTTPServer(("localhost", PORT), BridgeHandler).serve_forever()

if __name__ == "__main__":
    main()
