import streamlit as st
import sqlite3
from datetime import datetime, date, timedelta
import calendar
import json
import os
import re
import io

# Optional packages:
# pip install streamlit pandas openai streamlit-calendar streamlit-mic-recorder
try:
    import pandas as pd
except ImportError:
    pd = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    from streamlit_mic_recorder import speech_to_text
except ImportError:
    speech_to_text = None

DB = "todo_app.db"

st.set_page_config(
    page_title="Smart Todo AI",
    page_icon="✅",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ---------------- DATABASE ----------------
def db():
    conn = sqlite3.connect(DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

conn = db()

conn.executescript("""
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    due_date TEXT,
    due_time TEXT,
    priority TEXT DEFAULT 'Medium',
    category TEXT DEFAULT 'General',
    status TEXT DEFAULT 'Pending',
    points INTEGER DEFAULT 10,
    recurrence TEXT DEFAULT 'None',
    owner TEXT DEFAULT 'Me',
    shared_with TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS achievements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    description TEXT,
    unlocked_at TEXT
);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_date TEXT UNIQUE,
    completed_count INTEGER DEFAULT 0,
    earned_points INTEGER DEFAULT 0
);
""")
conn.commit()

# ---------------- HELPERS ----------------
PRIORITY_POINTS = {"Low": 5, "Medium": 10, "High": 20, "Urgent": 30}

def add_task(title, description="", due_date=None, due_time="",
             priority="Medium", category="General", recurrence="None",
             owner="Me", shared_with=""):
    points = PRIORITY_POINTS.get(priority, 10)
    conn.execute("""
        INSERT INTO tasks
        (title, description, due_date, due_time, priority, category,
         status, points, recurrence, owner, shared_with)
        VALUES (?, ?, ?, ?, ?, ?, 'Pending', ?, ?, ?, ?)
    """, (
        title.strip(), description.strip(),
        str(due_date) if due_date else None, due_time,
        priority, category, points, recurrence, owner, shared_with
    ))
    conn.commit()

def get_tasks(status=None):
    if status:
        return conn.execute(
            "SELECT * FROM tasks WHERE status=? ORDER BY due_date, due_time",
            (status,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM tasks ORDER BY status='Completed', due_date, due_time"
    ).fetchall()

def complete_task(task_id):
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row or row["status"] == "Completed":
        return

    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE tasks SET status='Completed', completed_at=? WHERE id=?",
        (now, task_id)
    )

    today = str(date.today())
    existing = conn.execute(
        "SELECT * FROM activity WHERE activity_date=?", (today,)
    ).fetchone()

    if existing:
        conn.execute("""
            UPDATE activity
            SET completed_count=completed_count+1,
                earned_points=earned_points+?
            WHERE activity_date=?
        """, (row["points"], today))
    else:
        conn.execute("""
            INSERT INTO activity(activity_date, completed_count, earned_points)
            VALUES (?, 1, ?)
        """, (today, row["points"]))

    # Create next recurring task.
    recurrence = row["recurrence"]
    if recurrence != "None":
        old_due = row["due_date"]
        if old_due:
            try:
                d = datetime.strptime(old_due, "%Y-%m-%d").date()
                if recurrence == "Daily":
                    next_d = d + timedelta(days=1)
                elif recurrence == "Weekly":
                    next_d = d + timedelta(days=7)
                elif recurrence == "Monthly":
                    next_d = d + timedelta(days=30)
                else:
                    next_d = None

                if next_d:
                    add_task(
                        row["title"], row["description"], next_d,
                        row["due_time"], row["priority"], row["category"],
                        row["recurrence"], row["owner"], row["shared_with"]
                    )
            except ValueError:
                pass

    conn.commit()
    update_achievements()

def delete_task(task_id):
    conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()

def update_achievements():
    completed = conn.execute(
        "SELECT COUNT(*) c FROM tasks WHERE status='Completed'"
    ).fetchone()["c"]

    total_points = conn.execute(
        "SELECT COALESCE(SUM(earned_points),0) p FROM activity"
    ).fetchone()["p"]

    achievements = [
        ("First Step", "Complete your first task.", completed >= 1),
        ("Task Master", "Complete 10 tasks.", completed >= 10),
        ("Task Hero", "Complete 25 tasks.", completed >= 25),
        ("Point Collector", "Earn 100 points.", total_points >= 100),
        ("Point Champion", "Earn 500 points.", total_points >= 500),
    ]

    for name, desc, unlocked in achievements:
        if unlocked:
            conn.execute("""
                INSERT OR IGNORE INTO achievements(name, description, unlocked_at)
                VALUES (?, ?, ?)
            """, (name, desc, datetime.now().isoformat(timespec="seconds")))
    conn.commit()

def calculate_streak():
    rows = conn.execute("""
        SELECT activity_date FROM activity
        WHERE completed_count > 0
        ORDER BY activity_date DESC
    """).fetchall()

    active = {r["activity_date"] for r in rows}
    if not active:
        return 0

    current = date.today()
    # If nothing was completed today, allow streak to continue from yesterday
    # for display purposes.
    if str(current) not in active:
        current -= timedelta(days=1)

    streak = 0
    while str(current) in active:
        streak += 1
        current -= timedelta(days=1)
    return streak

def smart_priority(task):
    """Simple rule-based smart prioritization."""
    score = PRIORITY_POINTS.get(task["priority"], 10)

    if task["due_date"]:
        try:
            due = datetime.strptime(task["due_date"], "%Y-%m-%d").date()
            days = (due - date.today()).days
            if days < 0:
                score += 40
            elif days == 0:
                score += 30
            elif days == 1:
                score += 20
            elif days <= 3:
                score += 10
        except ValueError:
            pass

    if task["category"].lower() in {"exam", "project", "work", "college"}:
        score += 5

    if score >= 60:
        return "🔴 Critical"
    if score >= 40:
        return "🟠 High"
    if score >= 25:
        return "🟡 Medium"
    return "🟢 Low"

def ai_assistant(prompt):
    api_key = st.session_state.get("openai_key") or os.getenv("OPENAI_API_KEY")

    if OpenAI and api_key:
        try:
            client = OpenAI(api_key=api_key)
            response = client.responses.create(
                model="gpt-4.1-mini",
                input=(
                    "You are a productivity assistant. Give concise, practical "
                    "task planning advice. User request:\n" + prompt
                )
            )
            return response.output_text
        except Exception as e:
            return f"AI service error: {e}"

    # Offline fallback: useful without an API key.
    p = prompt.lower()
    if "priorit" in p:
        return ("Prioritize tasks using: deadline urgency → importance → "
                "effort. Finish urgent, high-impact tasks first.")
    if "plan" in p:
        return ("Try a 3-level plan: 1 major task, 2 medium tasks, and "
                "small tasks for the day. Add breaks between focused sessions.")
    if "break" in p:
        return "For a long task, use 25–50 minute focus sessions followed by 5–10 minute breaks."
    return ("I can help break a large task into smaller steps, suggest priorities, "
            "and create a daily plan. Add an OpenAI API key in the sidebar for AI-powered answers.")

def parse_voice_text(text):
    """Very simple voice command parser."""
    if not text:
        return

    clean = text.strip()
    lower = clean.lower()

    priority = "Medium"
    if "urgent" in lower:
        priority = "Urgent"
    elif "high priority" in lower or "important" in lower:
        priority = "High"
    elif "low priority" in lower:
        priority = "Low"

    recurrence = "None"
    if "every day" in lower or "daily" in lower:
        recurrence = "Daily"
    elif "every week" in lower or "weekly" in lower:
        recurrence = "Weekly"
    elif "every month" in lower or "monthly" in lower:
        recurrence = "Monthly"

    # Remove common command words.
    title = re.sub(
        r"\b(add|create|new|task|remind me to|reminder to)\b",
        "",
        clean,
        flags=re.I
    ).strip(" ,.-")

    add_task(
        title=title or clean,
        priority=priority,
        recurrence=recurrence,
        due_date=date.today()
    )

# ---------------- SIDEBAR ----------------
with st.sidebar:
    st.title("✅ Smart Todo AI")
    st.caption("Your intelligent productivity workspace")

    page = st.radio(
        "Navigate",
        [
            "🏠 Dashboard",
            "➕ Add Task",
            "📋 Tasks",
            "📅 Calendar",
            "🤖 AI Assistant",
            "🎤 Voice Task",
            "🏆 Achievements",
            "👥 Shared Tasks",
        ]
    )

    st.divider()
    st.subheader("⚙️ Settings")
    st.session_state["openai_key"] = st.text_input(
        "OpenAI API key (optional)",
        type="password",
        help="Only needed for the AI-powered assistant."
    )

# ---------------- DASHBOARD ----------------
if page == "🏠 Dashboard":
    st.title("📊 Productivity Dashboard")
    st.write("See what needs attention and how your productivity is improving.")

    all_tasks = get_tasks()
    pending = [t for t in all_tasks if t["status"] == "Pending"]
    completed = [t for t in all_tasks if t["status"] == "Completed"]

    points = conn.execute(
        "SELECT COALESCE(SUM(earned_points),0) p FROM activity"
    ).fetchone()["p"]
    streak = calculate_streak()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📋 Pending", len(pending))
    c2.metric("✅ Completed", len(completed))
    c3.metric("⭐ Points", points)
    c4.metric("🔥 Streak", f"{streak} days")

    st.divider()

    left, right = st.columns([1.3, 1])

    with left:
        st.subheader("🧠 Smart Task Prioritization")
        smart = []
        for task in pending:
            smart.append({
                "Task": task["title"],
                "Due": task["due_date"] or "No date",
                "Priority": task["priority"],
                "Smart Level": smart_priority(task),
                "Points": task["points"]
            })

        smart.sort(key=lambda x: x["Points"], reverse=True)
        if smart:
            if pd:
                st.dataframe(pd.DataFrame(smart), use_container_width=True, hide_index=True)
            else:
                for item in smart:
                    st.write(item)
        else:
            st.success("No pending tasks 🎉")

    with right:
        st.subheader("📈 Completion Overview")
        if pd:
            chart_data = pd.DataFrame([
                {"Status": "Completed", "Count": len(completed)},
                {"Status": "Pending", "Count": len(pending)}
            ]).set_index("Status")
            st.bar_chart(chart_data)
        st.info(
            "💡 Tip: Completing high-priority tasks earns more points. "
            "Keep your daily streak alive!"
        )

    st.subheader("⏰ Today's Reminders")
    today = str(date.today())
    today_tasks = conn.execute("""
        SELECT * FROM tasks
        WHERE due_date=? AND status='Pending'
        ORDER BY due_time
    """, (today,)).fetchall()

    if today_tasks:
        for task in today_tasks:
            time_text = task["due_time"] or "Any time"
            st.warning(f"🔔 **{time_text}** — {task['title']} ({task['priority']})")
    else:
        st.success("No pending tasks for today.")

# ---------------- ADD TASK ----------------
elif page == "➕ Add Task":
    st.title("➕ Create a Task")

    with st.form("add_task_form", clear_on_submit=True):
        title = st.text_input("Task title *", placeholder="Example: Complete Python assignment")
        description = st.text_area("Description")
        c1, c2 = st.columns(2)
        due_date = c1.date_input("Due date", value=date.today())
        due_time = c2.time_input("Due time", value=datetime.now().time().replace(second=0, microsecond=0))

        c3, c4 = st.columns(2)
        priority = c3.selectbox("Priority", ["Low", "Medium", "High", "Urgent"], index=1)
        category = c4.selectbox(
            "Category",
            ["General", "College", "Project", "Work", "Personal", "Exam", "Health"]
        )

        recurrence = st.selectbox(
            "Recurring task",
            ["None", "Daily", "Weekly", "Monthly"]
        )

        shared_with = st.text_input(
            "Share with (optional)",
            placeholder="friend@example.com"
        )

        submitted = st.form_submit_button("🚀 Add Task", use_container_width=True)

        if submitted:
            if not title.strip():
                st.error("Please enter a task title.")
            else:
                add_task(
                    title, description, due_date, due_time.strftime("%H:%M"),
                    priority, category, recurrence, "Me", shared_with
                )
                st.success("Task created successfully! 🎉")

# ---------------- TASKS ----------------
elif page == "📋 Tasks":
    st.title("📋 My Tasks")

    filter_status = st.selectbox(
        "Filter",
        ["All", "Pending", "Completed"]
    )

    tasks = get_tasks(None if filter_status == "All" else filter_status)

    if not tasks:
        st.info("No tasks found.")
    else:
        for task in tasks:
            with st.container(border=True):
                c1, c2, c3 = st.columns([0.08, 0.72, 0.2])

                with c1:
                    done = task["status"] == "Completed"
                    if not done:
                        if st.button("☐", key=f"done_{task['id']}"):
                            complete_task(task["id"])
                            st.rerun()
                    else:
                        st.write("✅")

                with c2:
                    title = task["title"]
                    if done:
                        st.markdown(f"~~**{title}**~~")
                    else:
                        st.markdown(f"**{title}**")

                    details = []
                    if task["due_date"]:
                        details.append(f"📅 {task['due_date']}")
                    if task["due_time"]:
                        details.append(f"⏰ {task['due_time']}")
                    details.append(f"🔥 {task['priority']}")
                    details.append(f"⭐ {task['points']} pts")
                    if task["recurrence"] != "None":
                        details.append(f"🔁 {task['recurrence']}")
                    if task["shared_with"]:
                        details.append(f"👥 {task['shared_with']}")

                    st.caption(" | ".join(details))
                    if task["description"]:
                        st.write(task["description"])

                with c3:
                    if st.button("🗑️ Delete", key=f"del_{task['id']}"):
                        delete_task(task["id"])
                        st.rerun()

# ---------------- CALENDAR ----------------
elif page == "📅 Calendar":
    st.title("📅 Calendar View")

    year = st.selectbox("Year", list(range(date.today().year - 1, date.today().year + 3)),
                        index=1)
    month = st.selectbox(
        "Month",
        list(range(1, 13)),
        index=date.today().month - 1,
        format_func=lambda x: calendar.month_name[x]
    )

    st.subheader(f"{calendar.month_name[month]} {year}")

    month_tasks = conn.execute("""
        SELECT * FROM tasks
        WHERE due_date LIKE ?
        ORDER BY due_date, due_time
    """, (f"{year:04d}-{month:02d}-%",)).fetchall()

    by_day = {}
    for t in month_tasks:
        by_day.setdefault(t["due_date"], []).append(t)

    cal = calendar.monthcalendar(year, month)
    weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    cols = st.columns(7)
    for i, name in enumerate(weekdays):
        cols[i].markdown(f"**{name}**")

    for week in cal:
        cols = st.columns(7)
        for i, day_num in enumerate(week):
            with cols[i]:
                if day_num == 0:
                    st.write("")
                    continue

                d = f"{year:04d}-{month:02d}-{day_num:02d}"
                is_today = d == str(date.today())
                st.markdown(f"### {'📌 ' if is_today else ''}{day_num}")

                for task in by_day.get(d, []):
                    icon = "✅" if task["status"] == "Completed" else "📝"
                    st.caption(f"{icon} {task['title']}")

# ---------------- AI ----------------
elif page == "🤖 AI Assistant":
    st.title("🤖 AI Task Assistant")

    st.write(
        "Ask for a daily plan, task breakdown, prioritization, study schedule, "
        "or productivity advice."
    )

    prompt = st.text_area(
        "What do you need help with?",
        placeholder="Example: I have 5 college tasks and an exam tomorrow. Help me plan today."
    )

    if st.button("✨ Ask AI", use_container_width=True):
        if prompt.strip():
            with st.spinner("Thinking..."):
                answer = ai_assistant(prompt)
            st.markdown("### 💡 Assistant")
            st.write(answer)
        else:
            st.warning("Enter a question first.")

    st.divider()
    st.subheader("Quick AI actions")
    q1, q2, q3 = st.columns(3)

    if q1.button("📅 Plan my day"):
        pending = get_tasks("Pending")
        names = ", ".join(t["title"] for t in pending[:10]) or "no pending tasks"
        st.info(ai_assistant(f"Create a practical plan for these tasks: {names}"))

    if q2.button("🧩 Break down a task"):
        st.info(ai_assistant("How can I break a large college project into small tasks?"))

    if q3.button("🎯 Improve productivity"):
        st.info(ai_assistant("Give me five practical productivity improvements for a student."))

# ---------------- VOICE ----------------
elif page == "🎤 Voice Task":
    st.title("🎤 Voice Task Creation")

    if speech_to_text is None:
        st.warning(
            "Voice package is not installed. Run:\n\n"
            "`pip install streamlit-mic-recorder`\n\n"
            "Then restart Streamlit."
        )
    else:
        st.write("Click the microphone and say something like: “Create task finish Python project tomorrow high priority.”")

        voice_text = speech_to_text(
            language="en",
            start_prompt="🎙️ Start speaking",
            stop_prompt="⏹️ Stop",
            use_container_width=True,
            key="voice_input"
        )

        if voice_text:
            st.success(f"Heard: {voice_text}")
            if st.button("➕ Create Task From Voice"):
                parse_voice_text(voice_text)
                st.success("Task created from your voice!")
                st.rerun()

# ---------------- ACHIEVEMENTS ----------------
elif page == "🏆 Achievements":
    st.title("🏆 Points & Achievements")

    update_achievements()

    points = conn.execute(
        "SELECT COALESCE(SUM(earned_points),0) p FROM activity"
    ).fetchone()["p"]
    completed = conn.execute(
        "SELECT COUNT(*) c FROM tasks WHERE status='Completed'"
    ).fetchone()["c"]
    streak = calculate_streak()

    c1, c2, c3 = st.columns(3)
    c1.metric("⭐ Total Points", points)
    c2.metric("✅ Tasks Completed", completed)
    c3.metric("🔥 Current Streak", f"{streak} days")

    st.subheader("Badges")

    badges = conn.execute(
        "SELECT * FROM achievements ORDER BY unlocked_at DESC"
    ).fetchall()

    all_badges = [
        ("First Step", "Complete your first task."),
        ("Task Master", "Complete 10 tasks."),
        ("Task Hero", "Complete 25 tasks."),
        ("Point Collector", "Earn 100 points."),
        ("Point Champion", "Earn 500 points."),
    ]

    unlocked_names = {b["name"] for b in badges}

    for name, desc in all_badges:
        if name in unlocked_names:
            st.success(f"🏅 **{name}** — {desc}")
        else:
            st.info(f"🔒 **{name}** — {desc}")

# ---------------- SHARED TASKS ----------------
elif page == "👥 Shared Tasks":
    st.title("👥 Shared Tasks")

    st.info(
        "This local version stores the person/email you share with. "
        "For real multi-user collaboration, connect it to a hosted database "
        "and authentication system."
    )

    shared = conn.execute("""
        SELECT * FROM tasks
        WHERE shared_with IS NOT NULL AND TRIM(shared_with) != ''
        ORDER BY due_date
    """).fetchall()

    if not shared:
        st.write("No shared tasks yet.")
    else:
        for task in shared:
            status_icon = "✅" if task["status"] == "Completed" else "👥"
            st.markdown(
                f"{status_icon} **{task['title']}** — "
                f"Shared with: `{task['shared_with']}` — "
                f"Due: `{task['due_date'] or 'No date'}`"
            )

# ---------------- FOOTER ----------------
st.divider()
st.caption(
    "Smart Todo AI • Streamlit + SQLite • "
    "AI and voice features are optional."
)