#!/usr/bin/env python3
"""User Profile Engine — computes personalized user statistics from task history."""

import sqlite3
from datetime import datetime, timedelta

CATEGORY_EMOJI = {
    "学习": "📚", "工作": "💼", "项目": "🚀",
    "生活": "🏠", "内容创作": "🎨", "会议": "💬", "提醒": "🔔",
}


def build_profile_summary(db_path: str, days: int = 30) -> str:
    """
    Build a compact user profile summary for injection into AI prompts.
    
    Returns a Chinese text block describing the user's work habits,
    duration accuracy per category, preferred hours, etc.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    
    # ── Total tasks ──
    c.execute(
        "SELECT COUNT(*) AS cnt FROM tasks WHERE created_at >= ?",
        (cutoff,)
    )
    total = c.fetchone()["cnt"]
    
    if total == 0:
        conn.close()
        return "（暂无历史数据，使用默认估算）"
    
    # ── Completed tasks ──
    c.execute(
        "SELECT COUNT(*) AS cnt FROM tasks WHERE status = 'completed' AND created_at >= ?",
        (cutoff,)
    )
    completed = c.fetchone()["cnt"]
    
    # ── Priority completion rate ──
    c.execute(
        """SELECT priority, COUNT(*) AS cnt 
           FROM tasks 
           WHERE created_at >= ? AND priority IS NOT NULL 
           GROUP BY priority""",
        (cutoff,)
    )
    priority_stats = {}
    for row in c.fetchall():
        priority_stats[row["priority"]] = row["cnt"]
    
    c.execute(
        """SELECT priority, COUNT(*) AS cnt 
           FROM tasks 
           WHERE status = 'completed' AND created_at >= ? AND priority IS NOT NULL 
           GROUP BY priority""",
        (cutoff,)
    )
    priority_completed = {}
    for row in c.fetchall():
        priority_completed[row["priority"]] = row["cnt"]
    
    # ── Category distribution ──
    c.execute(
        """SELECT category, COUNT(*) AS cnt 
           FROM tasks 
           WHERE created_at >= ? AND category IS NOT NULL AND category != ''
           GROUP BY category 
           ORDER BY cnt DESC""",
        (cutoff,)
    )
    top_categories = c.fetchall()[:5]
    
    # ── Duration accuracy per category ──
    c.execute(
        """SELECT category, 
                  AVG(estimated_duration_minutes) AS avg_est,
                  AVG(actual_duration_minutes) AS avg_act,
                  COUNT(*) AS cnt
           FROM tasks 
           WHERE status = 'completed' 
             AND estimated_duration_minutes IS NOT NULL 
             AND actual_duration_minutes IS NOT NULL
             AND created_at >= ?
             AND category IS NOT NULL AND category != ''
           GROUP BY category
           HAVING cnt >= 2""",
        (cutoff,)
    )
    duration_accuracy = []
    for row in c.fetchall():
        ratio = row["avg_act"] / row["avg_est"] if row["avg_est"] > 0 else 1.0
        duration_accuracy.append({
            "category": row["category"],
            "ratio": ratio,
            "avg_est": row["avg_est"],
            "avg_act": row["avg_act"],
            "cnt": row["cnt"],
        })
    
    # ── Preferred working hours (from completed tasks) ──
    c.execute(
        """SELECT scheduled_start 
           FROM tasks 
           WHERE status = 'completed' 
             AND scheduled_start IS NOT NULL
             AND created_at >= ?""",
        (cutoff,)
    )
    hour_counts = {}
    for row in c.fetchall():
        try:
            # Extract hour from ISO datetime
            ts = row["scheduled_start"]
            if "T" in ts:
                hour = int(ts[11:13])
                hour_counts[hour] = hour_counts.get(hour, 0) + 1
        except (ValueError, IndexError):
            pass
    
    # Find top 3 hours
    top_hours = sorted(hour_counts.items(), key=lambda x: -x[1])[:3]
    
    # ── Day-of-week distribution ──
    c.execute(
        """SELECT scheduled_start 
           FROM tasks 
           WHERE status = 'completed' 
             AND scheduled_start IS NOT NULL
             AND created_at >= ?""",
        (cutoff,)
    )
    dow_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    dow_counts = {}
    for row in c.fetchall():
        try:
            ts = row["scheduled_start"]
            if "T" in ts:
                d = datetime.fromisoformat(ts[:19]) if len(ts) >= 19 else datetime.strptime(ts[:10], "%Y-%m-%d")
                dow = d.weekday()
                dow_counts[dow] = dow_counts.get(dow, 0) + 1
        except (ValueError, IndexError):
            pass
    
    top_dows = sorted(dow_counts.items(), key=lambda x: -x[1])[:3]
    
    # ── Reschedule rate ──
    c.execute(
        """SELECT COUNT(DISTINCT task_id) AS cnt 
           FROM task_events 
           WHERE event_type = 'rescheduled' 
             AND created_at >= ?""",
        (cutoff,)
    )
    rescheduled = c.fetchone()["cnt"]
    
    conn.close()
    
    # ── Build summary text ──
    lines = []
    lines.append(f"过去{days}天你创建了{total}个任务，完成了{completed}个（完成率{completed * 100 // max(total, 1)}%）。")
    
    # Priority
    if priority_stats:
        pri_parts = []
        for p in ["P1", "P2", "P3", "P4"]:
            if p in priority_stats:
                done = priority_completed.get(p, 0)
                rate = done * 100 // priority_stats[p]
                pri_parts.append(f"{p}:{rate}%")
        lines.append("各优先级完成率：" + " ".join(pri_parts) + "。")
    
    # Duration accuracy
    if duration_accuracy:
        dur_parts = []
        for item in duration_accuracy:
            emoji = CATEGORY_EMOJI.get(item["category"], "")
            if item["ratio"] > 1.15:
                direction = f"多花{int((item['ratio'] - 1) * 100)}%"
            elif item["ratio"] < 0.85:
                direction = f"少花{int((1 - item['ratio']) * 100)}%"
            else:
                direction = "基本准确"
            dur_parts.append(f"{emoji}{item['category']}类{direction}")
        lines.append("分类型耗时偏差（vs AI预估）：" + "；".join(dur_parts) + "。")
    
    # Preferred hours
    if top_hours:
        hour_strs = [f"{h}:00-{h+1}:00（{cnt}个任务）" for h, cnt in top_hours]
        lines.append("最活跃时段：" + "、".join(hour_strs) + "。")
    
    # Day of week
    if top_dows:
        dow_strs = [f"{dow_names[d]}（{cnt}个）" for d, cnt in top_dows]
        lines.append("最高产日子：" + "、".join(dow_strs) + "。")
    
    # Reschedule
    if rescheduled > 0:
        lines.append(f"你有{rescheduled}个任务曾被重新安排，安排时间时请尽量避开冲突。")
    
    return "\n".join(lines)


def inject_profile_into_prompt(prompt_template: str, profile_text: str) -> str:
    """
    Insert the user profile summary into an AI prompt template.
    
    Finds the best insertion point (after user background, before task instructions)
    and adds the profile block.
    """
    # If template already has a {user_profile} placeholder, use it
    if "{user_profile}" in prompt_template:
        return prompt_template.replace("{user_profile}", profile_text)
    
    # Otherwise, inject after "用户背景" sections
    insertion_marker = "必须严格按照以下JSON格式返回"
    if insertion_marker in prompt_template:
        profile_block = f"\n\n[用户画像 — 基于你的历史数据]\n{profile_text}\n\n{insertion_marker}"
        return prompt_template.replace(insertion_marker, profile_block)
    
    # Fallback: prepend
    return f"[用户画像]\n{profile_text}\n\n{prompt_template}"


def get_detailed_stats(db_path: str, days: int = 30) -> dict:
    """Return detailed stats as a dict (for frontend display)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    
    # Total & completed
    c.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE created_at >= ?", (cutoff,))
    total = c.fetchone()["cnt"]
    
    c.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE status = 'completed' AND created_at >= ?", (cutoff,))
    completed = c.fetchone()["cnt"]
    
    # Per category
    c.execute(
        """SELECT category, COUNT(*) AS cnt 
           FROM tasks WHERE created_at >= ? AND category IS NOT NULL AND category != ''
           GROUP BY category""",
        (cutoff,)
    )
    by_category = {r["category"]: r["cnt"] for r in c.fetchall()}
    
    # Per priority
    c.execute(
        """SELECT priority, COUNT(*) AS cnt 
           FROM tasks WHERE created_at >= ? AND priority IS NOT NULL
           GROUP BY priority""",
        (cutoff,)
    )
    by_priority = {r["priority"]: r["cnt"] for r in c.fetchall()}
    
    # Hour heatmap
    c.execute(
        """SELECT scheduled_start FROM tasks 
           WHERE status = 'completed' AND scheduled_start IS NOT NULL AND created_at >= ?""",
        (cutoff,)
    )
    hour_heatmap = [0] * 24
    for row in c.fetchall():
        try:
            ts = row["scheduled_start"]
            if "T" in ts:
                h = int(ts[11:13])
                hour_heatmap[h] += 1
        except (ValueError, IndexError):
            pass
    
    # Daily task count (last 7 days)
    daily_counts = {}
    for i in range(7):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        c.execute(
            """SELECT COUNT(*) AS cnt FROM tasks 
               WHERE created_at >= ? AND created_at < ?""",
            (d + "T00:00:00", d + "T23:59:59")
        )
        daily_counts[d] = c.fetchone()["cnt"]
    
    conn.close()
    
    return {
        "total_tasks": total,
        "completed_tasks": completed,
        "completion_rate": round(completed / max(total, 1) * 100, 1),
        "by_category": by_category,
        "by_priority": by_priority,
        "hour_heatmap": hour_heatmap,
        "daily_counts": daily_counts,
        "days": days,
    }
