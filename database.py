"""SQLiteデータベースの初期化とヘルパー関数"""
import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "kintai.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初回起動時にテーブルを作成し、サンプルユーザーを登録"""
    conn = get_conn()
    cur = conn.cursor()

    # 従業員テーブル
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login_id TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'employee',
            leave_days INTEGER NOT NULL DEFAULT 10
        )
    """)

    # 後方互換：既存DBに列を追加
    for sql in [
        "ALTER TABLE users ADD COLUMN leave_days INTEGER NOT NULL DEFAULT 10",
        "ALTER TABLE users ADD COLUMN department_id INTEGER",
        "ALTER TABLE users ADD COLUMN is_approver INTEGER NOT NULL DEFAULT 0",
        # 従業員タイプ・スケジュール設定
        "ALTER TABLE users ADD COLUMN emp_type TEXT NOT NULL DEFAULT 'seishain'",
        "ALTER TABLE users ADD COLUMN scheduled_start TEXT DEFAULT '09:00'",
        "ALTER TABLE users ADD COLUMN scheduled_end TEXT DEFAULT '18:15'",
        "ALTER TABLE users ADD COLUMN auto_break_minutes INTEGER NOT NULL DEFAULT 75",
        # 半休に「有給 or 公休」区分を付与
        "ALTER TABLE requests ADD COLUMN leave_kind TEXT",
    ]:
        try:
            cur.execute(sql)
        except sqlite3.OperationalError:
            pass

    # 特別休日テーブル（お正月・お盆など期間指定）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS special_holidays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL
        )
    """)

    # 初回の特別休日を投入
    cur.execute("SELECT COUNT(*) FROM special_holidays")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO special_holidays (name, start_date, end_date) VALUES (?,?,?)",
            [
                ("お正月", "01-01", "01-03"),
                ("お盆",   "08-13", "08-16"),
            ]
        )

    # 部署テーブル
    cur.execute("""
        CREATE TABLE IF NOT EXISTS departments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
    """)

    # 初期部署
    cur.execute("SELECT COUNT(*) FROM departments")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO departments (name) VALUES (?)",
            [("宿泊事業部",), ("WEB事業部",)]
        )

    # 打刻テーブル
    cur.execute("""
        CREATE TABLE IF NOT EXISTS punches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            punch_type TEXT NOT NULL,
            punched_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # 旧：有給申請テーブル（後方互換のため残す）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS leaves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            leave_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            requested_at TEXT NOT NULL,
            reviewed_at TEXT,
            note TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # 統合申請テーブル（7種類の申請をここに集約）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            req_type TEXT NOT NULL,
            target_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            requested_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewer_id INTEGER,
            note TEXT,
            half_period TEXT,
            delay_minutes INTEGER,
            start_time TEXT,
            end_time TEXT,
            fix_clock_in TEXT,
            fix_clock_out TEXT,
            fix_break_in TEXT,
            fix_break_out TEXT,
            transport_route TEXT,
            transport_amount INTEGER
        )
    """)

    # 旧leavesをrequestsに移行（requestsが空で、leavesにデータがあれば）
    cnt = cur.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    if cnt == 0:
        legacy = cur.execute("SELECT * FROM leaves").fetchall()
        for l in legacy:
            cur.execute(
                """INSERT INTO requests
                   (user_id, req_type, target_date, status, requested_at, reviewed_at, note)
                   VALUES (?, 'leave', ?, ?, ?, ?, ?)""",
                (l["user_id"], l["leave_date"], l["status"],
                 l["requested_at"], l["reviewed_at"], l["note"])
            )

    # サンプルユーザー
    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        samples = [
            ("admin", "admin123", "管理者", "admin", 0),
            ("tanaka", "pass123", "田中太郎", "employee", 10),
            ("suzuki", "pass123", "鈴木花子", "employee", 10),
            ("sato",   "pass123", "佐藤一郎", "employee", 10),
        ]
        cur.executemany(
            "INSERT INTO users (login_id, password, name, role, leave_days) VALUES (?,?,?,?,?)",
            samples
        )

    conn.commit()
    conn.close()


def find_user(login_id: str, password: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE login_id=? AND password=?",
        (login_id, password)
    ).fetchone()
    conn.close()
    return row


def get_user(user_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return row


def get_all_employees():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM users WHERE role != 'admin' ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


# --- 従業員タイプ・スケジュール ---
EMP_TYPES = {
    "seishain":        "社員",
    "honsha_arubaito": "本社アルバイト",
    "genba_arubaito":  "現場アルバイト",
}


def update_employee_profile(user_id: int, emp_type: str,
                            scheduled_start: str, scheduled_end: str,
                            auto_break_minutes: int):
    conn = get_conn()
    conn.execute(
        """UPDATE users SET emp_type=?, scheduled_start=?, scheduled_end=?,
                            auto_break_minutes=? WHERE id=?""",
        (emp_type, scheduled_start or None, scheduled_end or None,
         auto_break_minutes, user_id)
    )
    conn.commit()
    conn.close()


# --- 特別休日（お正月・お盆等） ---
def get_special_holidays():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM special_holidays ORDER BY start_date").fetchall()
    conn.close()
    return rows


def create_special_holiday(name: str, start_date: str, end_date: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO special_holidays (name, start_date, end_date) VALUES (?,?,?)",
        (name, start_date, end_date)
    )
    conn.commit()
    conn.close()


def delete_special_holiday(h_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM special_holidays WHERE id=?", (h_id,))
    conn.commit()
    conn.close()


# --- 部署 ---
def get_departments():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM departments ORDER BY id").fetchall()
    conn.close()
    return rows


def get_department(dep_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM departments WHERE id=?", (dep_id,)).fetchone()
    conn.close()
    return row


def create_department(name: str):
    conn = get_conn()
    try:
        conn.execute("INSERT INTO departments (name) VALUES (?)", (name,))
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "同じ名前の部署が既にあります"
    finally:
        conn.close()


def rename_department(dep_id: int, name: str):
    conn = get_conn()
    try:
        conn.execute("UPDATE departments SET name=? WHERE id=?", (name, dep_id))
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "同じ名前の部署が既にあります"
    finally:
        conn.close()


def delete_department(dep_id: int):
    """その部署の従業員は部署なしになる"""
    conn = get_conn()
    conn.execute("UPDATE users SET department_id=NULL, is_approver=0 WHERE department_id=?", (dep_id,))
    conn.execute("DELETE FROM departments WHERE id=?", (dep_id,))
    conn.commit()
    conn.close()


def get_approver_of(department_id: int):
    """部署の承認者（1人）を取得"""
    if not department_id:
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE department_id=? AND is_approver=1 LIMIT 1",
        (department_id,)
    ).fetchone()
    conn.close()
    return row


def update_user_department(user_id: int, department_id, is_approver: bool):
    """従業員の部署・承認者フラグを更新。承認者は部署内1人のみ許可"""
    conn = get_conn()
    if is_approver and department_id:
        # 同じ部署の他の承認者を解除
        conn.execute(
            "UPDATE users SET is_approver=0 WHERE department_id=? AND id!=?",
            (department_id, user_id)
        )
    conn.execute(
        "UPDATE users SET department_id=?, is_approver=? WHERE id=?",
        (department_id, 1 if is_approver else 0, user_id)
    )
    conn.commit()
    conn.close()


def create_user(login_id: str, password: str, name: str, role: str, leave_days: int):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO users (login_id, password, name, role, leave_days) VALUES (?,?,?,?,?)",
            (login_id, password, name, role, leave_days)
        )
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "同じログインIDが既に存在します"
    finally:
        conn.close()


def delete_user(user_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM punches WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM leaves WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=? AND role != 'admin'", (user_id,))
    conn.commit()
    conn.close()


def update_password(user_id: int, new_password: str):
    conn = get_conn()
    conn.execute("UPDATE users SET password=? WHERE id=?", (new_password, user_id))
    conn.commit()
    conn.close()


def verify_password(user_id: int, password: str) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM users WHERE id=? AND password=?", (user_id, password)
    ).fetchone()
    conn.close()
    return row is not None


def update_leave_days(user_id: int, days: int):
    conn = get_conn()
    conn.execute("UPDATE users SET leave_days=? WHERE id=?", (days, user_id))
    conn.commit()
    conn.close()


# --- 打刻 ---
def add_punch(user_id: int, punch_type: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO punches (user_id, punch_type, punched_at) VALUES (?,?,?)",
        (user_id, punch_type, datetime.now().isoformat(timespec="seconds"))
    )
    conn.commit()
    conn.close()


def get_today_punches(user_id: int):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM punches WHERE user_id=? AND punched_at LIKE ? ORDER BY punched_at",
        (user_id, f"{today}%")
    ).fetchall()
    conn.close()
    return rows


def get_day_punches(user_id: int, day_str: str):
    """指定日（YYYY-MM-DD）の打刻を取得"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM punches WHERE user_id=? AND punched_at LIKE ? ORDER BY punched_at",
        (user_id, f"{day_str}%")
    ).fetchall()
    conn.close()
    return rows


def delete_day_punches(user_id: int, day_str: str):
    conn = get_conn()
    conn.execute(
        "DELETE FROM punches WHERE user_id=? AND punched_at LIKE ?",
        (user_id, f"{day_str}%")
    )
    conn.commit()
    conn.close()


def add_punch_at(user_id: int, punch_type: str, punched_at_iso: str):
    """指定時刻の打刻を追加（管理者の修正用）"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO punches (user_id, punch_type, punched_at) VALUES (?,?,?)",
        (user_id, punch_type, punched_at_iso)
    )
    conn.commit()
    conn.close()


def get_monthly_punches(user_id: int, year: int, month: int):
    prefix = f"{year:04d}-{month:02d}"
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM punches WHERE user_id=? AND punched_at LIKE ? ORDER BY punched_at",
        (user_id, f"{prefix}%")
    ).fetchall()
    conn.close()
    return rows


# --- 申請（統合） ---
REQ_TYPES = {
    "leave":        "有給休暇",
    "half_leave":   "半休",
    "delay":        "電車遅延",
    "extra_work":   "臨時出勤",
    "punch_fix":    "打刻修正",
    "transport":    "交通費精算",
    "other":        "その他",
}


def create_request(user_id: int, req_type: str, target_date: str, note: str = "",
                   half_period=None, delay_minutes=None,
                   start_time=None, end_time=None,
                   fix_clock_in=None, fix_clock_out=None,
                   fix_break_in=None, fix_break_out=None,
                   transport_route=None, transport_amount=None,
                   leave_kind=None):
    conn = get_conn()
    conn.execute(
        """INSERT INTO requests
           (user_id, req_type, target_date, status, requested_at, note,
            half_period, delay_minutes, start_time, end_time,
            fix_clock_in, fix_clock_out, fix_break_in, fix_break_out,
            transport_route, transport_amount, leave_kind)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, req_type, target_date, "pending",
         datetime.now().isoformat(timespec="seconds"), note,
         half_period, delay_minutes, start_time, end_time,
         fix_clock_in, fix_clock_out, fix_break_in, fix_break_out,
         transport_route, transport_amount, leave_kind)
    )
    conn.commit()
    conn.close()


def get_request(req_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    conn.close()
    return row


def get_user_requests(user_id: int):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM requests WHERE user_id=? ORDER BY target_date DESC, id DESC",
        (user_id,)
    ).fetchall()
    conn.close()
    return rows


def get_pending_requests_for_department(dep_id: int):
    """指定部署の承認待ち申請"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT r.*, u.name as user_name FROM requests r
           JOIN users u ON r.user_id = u.id
           WHERE u.department_id=? AND r.status='pending'
           ORDER BY r.target_date ASC, r.id ASC""",
        (dep_id,)
    ).fetchall()
    conn.close()
    return rows


def get_all_pending_requests():
    conn = get_conn()
    rows = conn.execute(
        """SELECT r.*, u.name as user_name, d.name as department_name
           FROM requests r
           JOIN users u ON r.user_id = u.id
           LEFT JOIN departments d ON u.department_id = d.id
           WHERE r.status='pending'
           ORDER BY r.target_date ASC, r.id ASC"""
    ).fetchall()
    conn.close()
    return rows


def count_pending_requests_for_department(dep_id: int) -> int:
    conn = get_conn()
    row = conn.execute(
        """SELECT COUNT(*) FROM requests r
           JOIN users u ON r.user_id = u.id
           WHERE u.department_id=? AND r.status='pending'""",
        (dep_id,)
    ).fetchone()
    conn.close()
    return row[0]


def review_request(req_id: int, reviewer_id: int, status: str):
    conn = get_conn()
    conn.execute(
        "UPDATE requests SET status=?, reviewer_id=?, reviewed_at=? WHERE id=?",
        (status, reviewer_id, datetime.now().isoformat(timespec="seconds"), req_id)
    )
    conn.commit()
    conn.close()


def delete_request(req_id: int, user_id: int):
    """本人のみ pending 状態を取消可能"""
    conn = get_conn()
    conn.execute(
        "DELETE FROM requests WHERE id=? AND user_id=? AND status='pending'",
        (req_id, user_id)
    )
    conn.commit()
    conn.close()


def get_approved_leave_days(user_id: int, year: int = None, month: int = None):
    """有給取得日数（半休は0.5日としてカウント）"""
    conn = get_conn()
    if year and month:
        prefix = f"{year:04d}-{month:02d}"
        rows = conn.execute(
            """SELECT req_type FROM requests
               WHERE user_id=? AND status='approved' AND target_date LIKE ?
                 AND req_type IN ('leave','half_leave')""",
            (user_id, f"{prefix}%")
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT req_type FROM requests
               WHERE user_id=? AND status='approved'
                 AND req_type IN ('leave','half_leave')""",
            (user_id,)
        ).fetchall()
    conn.close()
    days = 0.0
    for r in rows:
        days += 0.5 if r["req_type"] == "half_leave" else 1.0
    return days


def get_approved_requests_in_month(user_id: int, year: int, month: int):
    prefix = f"{year:04d}-{month:02d}"
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM requests
           WHERE user_id=? AND status='approved' AND target_date LIKE ?""",
        (user_id, f"{prefix}%")
    ).fetchall()
    conn.close()
    return rows


def get_transport_expenses_in_month(year: int, month: int):
    """管理者用：月の交通費精算一覧"""
    prefix = f"{year:04d}-{month:02d}"
    conn = get_conn()
    rows = conn.execute(
        """SELECT r.*, u.name as user_name, d.name as department_name
           FROM requests r
           JOIN users u ON r.user_id = u.id
           LEFT JOIN departments d ON u.department_id = d.id
           WHERE r.req_type='transport' AND r.status='approved'
             AND r.target_date LIKE ?
           ORDER BY r.target_date""",
        (f"{prefix}%",)
    ).fetchall()
    conn.close()
    return rows


# --- 旧有給（互換のため残置） ---
def add_leave_request(user_id: int, leave_date: str, note: str = ""):
    conn = get_conn()
    conn.execute(
        """INSERT INTO leaves (user_id, leave_date, status, requested_at, note)
           VALUES (?,?,?,?,?)""",
        (user_id, leave_date, "pending",
         datetime.now().isoformat(timespec="seconds"), note)
    )
    conn.commit()
    conn.close()


def get_user_leaves(user_id: int):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM leaves WHERE user_id=? ORDER BY leave_date DESC",
        (user_id,)
    ).fetchall()
    conn.close()
    return rows


def get_approved_leaves_in_month(user_id: int, year: int, month: int):
    prefix = f"{year:04d}-{month:02d}"
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM leaves
           WHERE user_id=? AND status='approved' AND leave_date LIKE ?""",
        (user_id, f"{prefix}%")
    ).fetchall()
    conn.close()
    return rows


def count_approved_leaves(user_id: int) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM leaves WHERE user_id=? AND status='approved'",
        (user_id,)
    ).fetchone()
    conn.close()
    return row[0]


def get_pending_leaves():
    """管理者用：承認待ちの全申請"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT l.*, u.name as user_name FROM leaves l
           JOIN users u ON l.user_id = u.id
           WHERE l.status='pending'
           ORDER BY l.leave_date ASC"""
    ).fetchall()
    conn.close()
    return rows


def update_leave_status(leave_id: int, status: str):
    conn = get_conn()
    conn.execute(
        "UPDATE leaves SET status=?, reviewed_at=? WHERE id=?",
        (status, datetime.now().isoformat(timespec="seconds"), leave_id)
    )
    conn.commit()
    conn.close()


def delete_leave(leave_id: int, user_id: int):
    """本人のみ pending 状態の申請を取消可能"""
    conn = get_conn()
    conn.execute(
        "DELETE FROM leaves WHERE id=? AND user_id=? AND status='pending'",
        (leave_id, user_id)
    )
    conn.commit()
    conn.close()
