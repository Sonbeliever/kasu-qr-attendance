from __future__ import annotations

import io
import os
import re
import sqlite3
import uuid
import zipfile
from collections import defaultdict
from datetime import datetime
from functools import wraps
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import qrcode
from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


APP_NAME = "Smart Campus Management System"
APP_TZ = ZoneInfo("Africa/Lagos")
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_FILE = os.path.join(BASE_DIR, os.getenv("SMART_CAMPUS_DB", "attendance.db"))
STATIC_DIR = os.path.join(BASE_DIR, "static")
QR_DIR = os.path.join(STATIC_DIR, "qr")
PROFILE_UPLOAD_DIR = os.path.join(STATIC_DIR, "uploads", "profiles")
RECEIPT_UPLOAD_DIR = os.path.join(STATIC_DIR, "uploads", "receipts")
LOGO_PATH = os.path.join(BASE_DIR, "logo.jpg")
DEFAULT_AVATAR = "kasu.jpg"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
RECEIPT_EXTENSIONS = IMAGE_EXTENSIONS | {".pdf"}
VISIBLE_MODERATION_STATUSES = ("clean", "approved")
BAD_WORDS = {
    "abuse",
    "bastard",
    "crazy",
    "damn",
    "fool",
    "hate",
    "idiot",
    "kill",
    "nonsense",
    "scam",
    "stupid",
    "trash",
}
SPAM_PHRASES = {
    "buy now",
    "click here",
    "free money",
    "limited offer",
    "subscribe now",
    "visit my page",
    "whatsapp me",
}


app = Flask(__name__)
app.secret_key = os.getenv("SMART_CAMPUS_SECRET", "smart-campus-change-me")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024


def ensure_directories() -> None:
    os.makedirs(STATIC_DIR, exist_ok=True)
    os.makedirs(QR_DIR, exist_ok=True)
    os.makedirs(PROFILE_UPLOAD_DIR, exist_ok=True)
    os.makedirs(RECEIPT_UPLOAD_DIR, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now_local() -> datetime:
    return datetime.now(APP_TZ)


def now_iso() -> str:
    return now_local().isoformat(timespec="seconds")


def today_string() -> str:
    return now_local().strftime("%Y-%m-%d")


def safe_relative_path(relative_path: str) -> str:
    return relative_path.replace("\\", "/").lstrip("/")


def absolute_static_path(relative_path: str) -> str:
    normalized = safe_relative_path(relative_path)
    return os.path.join(STATIC_DIR, normalized.replace("/", os.sep))


def allowed_file(filename: str, allowed_extensions: set[str]) -> bool:
    ext = os.path.splitext(filename or "")[1].lower()
    return ext in allowed_extensions


def save_upload(file_storage, relative_folder: str, prefix: str, allowed_extensions: set[str]) -> str:
    if not file_storage or not file_storage.filename:
        raise ValueError("No file was uploaded.")

    original_name = secure_filename(file_storage.filename)
    if not allowed_file(original_name, allowed_extensions):
        raise ValueError("Unsupported file format.")

    _, extension = os.path.splitext(original_name)
    filename = f"{prefix}_{uuid.uuid4().hex[:12]}{extension.lower()}"
    relative_path = safe_relative_path(os.path.join(relative_folder, filename))
    destination = absolute_static_path(relative_path)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    file_storage.save(destination)
    return relative_path


def normalize_text(value: str | None) -> str:
    return (value or "").strip()


def normalize_username(value: str | None) -> str:
    return normalize_text(value).lower()


def normalize_email(value: str | None) -> str:
    return normalize_text(value).lower()


def normalize_matric(value: str | None) -> str:
    return re.sub(r"\s+", "", normalize_text(value)).upper()


def normalize_department_code(value: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", normalize_text(value)).strip("_")
    return cleaned.upper()


def initialize_db() -> None:
    ensure_directories()
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS departments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            code TEXT NOT NULL UNIQUE,
            description TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('super_admin', 'department_admin', 'student')),
            full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            phone TEXT NOT NULL,
            department_id INTEGER,
            matric_number TEXT UNIQUE,
            profile_photo TEXT,
            qr_token TEXT UNIQUE,
            qr_path TEXT,
            id_card_generated_at TEXT,
            id_card_reprint_count INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (department_id) REFERENCES departments(id)
        );

        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            department_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            title TEXT NOT NULL,
            semester TEXT,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(department_id, code),
            FOREIGN KEY (department_id) REFERENCES departments(id),
            FOREIGN KEY (created_by) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS attendance_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            department_id INTEGER NOT NULL,
            course_id INTEGER,
            session_type TEXT NOT NULL CHECK(session_type IN ('general', 'course')),
            title TEXT NOT NULL,
            session_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            started_by INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active', 'closed')) DEFAULT 'active',
            created_at TEXT NOT NULL,
            closed_at TEXT,
            FOREIGN KEY (department_id) REFERENCES departments(id),
            FOREIGN KEY (course_id) REFERENCES courses(id),
            FOREIGN KEY (started_by) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS attendance_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            course_id INTEGER,
            department_id INTEGER NOT NULL,
            attendance_type TEXT NOT NULL CHECK(attendance_type IN ('general', 'course')),
            marked_at TEXT NOT NULL,
            UNIQUE(session_id, student_id),
            FOREIGN KEY (session_id) REFERENCES attendance_sessions(id),
            FOREIGN KEY (student_id) REFERENCES accounts(id),
            FOREIGN KEY (course_id) REFERENCES courses(id),
            FOREIGN KEY (department_id) REFERENCES departments(id)
        );
        """
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS id_card_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            department_id INTEGER NOT NULL,
            receipt_path TEXT NOT NULL,
            amount INTEGER NOT NULL DEFAULT 1000,
            status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')) DEFAULT 'pending',
            note TEXT,
            requested_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by INTEGER,
            FOREIGN KEY (student_id) REFERENCES accounts(id),
            FOREIGN KEY (department_id) REFERENCES departments(id),
            FOREIGN KEY (reviewed_by) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS community_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author_id INTEGER NOT NULL,
            department_id INTEGER NOT NULL,
            post_type TEXT NOT NULL CHECK(post_type IN ('post', 'poll')) DEFAULT 'post',
            visibility TEXT NOT NULL CHECK(visibility IN ('department', 'all')) DEFAULT 'department',
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            moderation_status TEXT NOT NULL CHECK(moderation_status IN ('clean', 'flagged', 'approved', 'rejected')) DEFAULT 'clean',
            moderation_reason TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (author_id) REFERENCES accounts(id),
            FOREIGN KEY (department_id) REFERENCES departments(id)
        );

        CREATE TABLE IF NOT EXISTS community_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            department_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            moderation_status TEXT NOT NULL CHECK(moderation_status IN ('clean', 'flagged', 'approved', 'rejected')) DEFAULT 'clean',
            moderation_reason TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (post_id) REFERENCES community_posts(id) ON DELETE CASCADE,
            FOREIGN KEY (author_id) REFERENCES accounts(id),
            FOREIGN KEY (department_id) REFERENCES departments(id)
        );

        CREATE TABLE IF NOT EXISTS community_likes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            department_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(post_id, user_id),
            FOREIGN KEY (post_id) REFERENCES community_posts(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES accounts(id),
            FOREIGN KEY (department_id) REFERENCES departments(id)
        );

        CREATE TABLE IF NOT EXISTS community_poll_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            department_id INTEGER NOT NULL,
            option_text TEXT NOT NULL,
            FOREIGN KEY (post_id) REFERENCES community_posts(id) ON DELETE CASCADE,
            FOREIGN KEY (department_id) REFERENCES departments(id)
        );

        CREATE TABLE IF NOT EXISTS community_poll_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            option_id INTEGER NOT NULL,
            voter_id INTEGER NOT NULL,
            department_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(post_id, voter_id),
            FOREIGN KEY (post_id) REFERENCES community_posts(id) ON DELETE CASCADE,
            FOREIGN KEY (option_id) REFERENCES community_poll_options(id) ON DELETE CASCADE,
            FOREIGN KEY (voter_id) REFERENCES accounts(id),
            FOREIGN KEY (department_id) REFERENCES departments(id)
        );

        CREATE INDEX IF NOT EXISTS idx_accounts_role_department
            ON accounts(role, department_id);
        CREATE INDEX IF NOT EXISTS idx_courses_department
            ON courses(department_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_department_status
            ON attendance_sessions(department_id, status, session_date);
        CREATE INDEX IF NOT EXISTS idx_records_department_student
            ON attendance_records(department_id, student_id, marked_at);
        CREATE INDEX IF NOT EXISTS idx_posts_department_visibility
            ON community_posts(department_id, visibility, moderation_status, created_at);
        """
    )
    existing_super_admin = conn.execute(
        "SELECT id FROM accounts WHERE role = 'super_admin' LIMIT 1"
    ).fetchone()
    if not existing_super_admin:
        created_at = now_iso()
        conn.execute(
            """
            INSERT INTO accounts (
                username, password, role, full_name, email, phone,
                department_id, matric_number, profile_photo, qr_token, qr_path,
                id_card_generated_at, is_active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "superadmin",
                generate_password_hash("admin123"),
                "super_admin",
                "Head of School",
                "superadmin@campus.local",
                "08000000000",
                None,
                None,
                None,
                None,
                None,
                None,
                1,
                created_at,
                created_at,
            ),
        )
    conn.commit()
    conn.close()


def get_account(account_id: int | None) -> sqlite3.Row | None:
    if not account_id:
        return None
    conn = get_conn()
    row = conn.execute(
        """
        SELECT a.*, d.name AS department_name, d.code AS department_code
        FROM accounts a
        LEFT JOIN departments d ON d.id = a.department_id
        WHERE a.id = ?
        """,
        (account_id,),
    ).fetchone()
    conn.close()
    return row


def get_account_by_identifier(identifier: str | None) -> sqlite3.Row | None:
    normalized = normalize_text(identifier)
    if not normalized:
        return None

    username = normalize_username(normalized)
    email = normalize_email(normalized)
    matric = normalize_matric(normalized)
    conn = get_conn()
    row = conn.execute(
        """
        SELECT a.*, d.name AS department_name, d.code AS department_code
        FROM accounts a
        LEFT JOIN departments d ON d.id = a.department_id
        WHERE LOWER(a.username) = ?
           OR LOWER(a.email) = ?
           OR UPPER(COALESCE(a.matric_number, '')) = ?
        LIMIT 1
        """,
        (username, email, matric),
    ).fetchone()
    conn.close()
    return row


def get_department(department_id: int | None) -> sqlite3.Row | None:
    if not department_id:
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM departments WHERE id = ?",
        (department_id,),
    ).fetchone()
    conn.close()
    return row


def ensure_student_qr(account_id: int) -> sqlite3.Row:
    conn = get_conn()
    student = conn.execute(
        "SELECT id, matric_number, full_name, qr_token, qr_path FROM accounts WHERE id = ? AND role = 'student'",
        (account_id,),
    ).fetchone()
    if not student:
        conn.close()
        raise ValueError("Student record was not found.")

    qr_token = student["qr_token"] or f"SCMS:{uuid.uuid4().hex}"
    qr_path = student["qr_path"]
    needs_file = True
    if qr_path:
        needs_file = not os.path.exists(absolute_static_path(qr_path))

    if needs_file:
        safe_matric = re.sub(r"[^A-Za-z0-9]+", "_", student["matric_number"] or f"student_{student['id']}").strip("_")
        filename = f"{safe_matric}_{uuid.uuid4().hex[:8]}.png"
        qr_path = safe_relative_path(os.path.join("qr", filename))
        qr_image = qrcode.make(qr_token)
        qr_image.save(absolute_static_path(qr_path))

    conn.execute(
        "UPDATE accounts SET qr_token = ?, qr_path = ?, updated_at = ? WHERE id = ?",
        (qr_token, qr_path, now_iso(), account_id),
    )
    conn.commit()
    conn.close()
    refreshed = get_account(account_id)
    if not refreshed:
        raise ValueError("Student record was not found after QR update.")
    return refreshed


def current_user() -> sqlite3.Row | None:
    return getattr(g, "current_user", None)


@app.before_request
def load_current_user() -> None:
    g.current_user = get_account(session.get("user_id")) if session.get("user_id") else None


@app.context_processor
def inject_globals():
    return {
        "app_name": APP_NAME,
        "current_user": current_user(),
        "current_year": now_local().year,
    }


def login_user(account: sqlite3.Row) -> None:
    session.clear()
    session["user_id"] = account["id"]


def logout_user() -> None:
    session.clear()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def roles_required(*roles: str):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            if user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def can_access_department(user: sqlite3.Row, department_id: int | None) -> bool:
    if user["role"] == "super_admin":
        return True
    return bool(department_id and user["department_id"] == department_id)


def require_department_access(user: sqlite3.Row, department_id: int | None) -> None:
    if not can_access_department(user, department_id):
        abort(403)


def require_student_owner_or_admin(student_id: int) -> sqlite3.Row:
    user = current_user()
    if not user:
        abort(403)

    student = get_account(student_id)
    if not student or student["role"] != "student":
        abort(404)

    if user["role"] == "student" and user["id"] != student_id:
        abort(403)
    if user["role"] in {"department_admin", "super_admin"}:
        require_department_access(user, student["department_id"])
    return student


def moderate_text(*parts: str) -> tuple[str, str | None]:
    combined = " ".join(normalize_text(part).lower() for part in parts if part).strip()
    if not combined:
        return "clean", None

    offensive_hits = sorted(
        word for word in BAD_WORDS if re.search(rf"\b{re.escape(word)}\b", combined)
    )
    if offensive_hits:
        return "flagged", f"Flagged for offensive language: {', '.join(offensive_hits[:3])}"

    if any(phrase in combined for phrase in SPAM_PHRASES):
        return "flagged", "Flagged for suspected promotional or spam language."
    if combined.count("http://") + combined.count("https://") > 2:
        return "flagged", "Flagged for suspicious link volume."
    if re.search(r"(.)\1{7,}", combined):
        return "flagged", "Flagged for repetitive spam-like text."

    words = re.findall(r"[a-z0-9']+", combined)
    if len(words) >= 18:
        unique_ratio = len(set(words)) / max(1, len(words))
        if unique_ratio < 0.35:
            return "flagged", "Flagged for highly repetitive content."

    return "clean", None


def parse_department_for_request(user: sqlite3.Row, raw_department_id: str | None) -> int | None:
    if user["role"] == "super_admin":
        return int(raw_department_id) if raw_department_id else None
    return user["department_id"]


def get_departments() -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM departments ORDER BY name").fetchall()
    conn.close()
    return rows


def get_courses_for_department(department_id: int | None) -> list[sqlite3.Row]:
    if not department_id:
        return []
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT c.*, d.name AS department_name
        FROM courses c
        JOIN departments d ON d.id = c.department_id
        WHERE c.department_id = ?
        ORDER BY c.code, c.title
        """,
        (department_id,),
    ).fetchall()
    conn.close()
    return rows


def get_accessible_departments_for_user(user: sqlite3.Row) -> list[sqlite3.Row]:
    if user["role"] == "super_admin":
        return get_departments()
    department = get_department(user["department_id"])
    return [department] if department else []


def get_active_session_for_department(department_id: int | None) -> sqlite3.Row | None:
    if not department_id:
        return None
    conn = get_conn()
    row = conn.execute(
        """
        SELECT s.*, c.code AS course_code, c.title AS course_title, d.name AS department_name,
               a.full_name AS started_by_name
        FROM attendance_sessions s
        JOIN departments d ON d.id = s.department_id
        LEFT JOIN courses c ON c.id = s.course_id
        JOIN accounts a ON a.id = s.started_by
        WHERE s.department_id = ? AND s.status = 'active'
        ORDER BY s.created_at DESC
        LIMIT 1
        """,
        (department_id,),
    ).fetchone()
    conn.close()
    return row


def get_recent_sessions(user: sqlite3.Row, limit: int = 10) -> list[sqlite3.Row]:
    conn = get_conn()
    if user["role"] == "super_admin":
        rows = conn.execute(
            """
            SELECT s.*, d.name AS department_name, c.code AS course_code, c.title AS course_title
            FROM attendance_sessions s
            JOIN departments d ON d.id = s.department_id
            LEFT JOIN courses c ON c.id = s.course_id
            ORDER BY s.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT s.*, d.name AS department_name, c.code AS course_code, c.title AS course_title
            FROM attendance_sessions s
            JOIN departments d ON d.id = s.department_id
            LEFT JOIN courses c ON c.id = s.course_id
            WHERE s.department_id = ?
            ORDER BY s.created_at DESC
            LIMIT ?
            """,
            (user["department_id"], limit),
        ).fetchall()
    conn.close()
    return rows


def get_recent_attendance(user: sqlite3.Row, limit: int = 12) -> list[sqlite3.Row]:
    conn = get_conn()
    if user["role"] == "super_admin":
        rows = conn.execute(
            """
            SELECT ar.*, s.title AS session_title, s.session_date, s.session_type,
                   st.full_name AS student_name, st.matric_number,
                   d.name AS department_name, c.code AS course_code
            FROM attendance_records ar
            JOIN attendance_sessions s ON s.id = ar.session_id
            JOIN accounts st ON st.id = ar.student_id
            JOIN departments d ON d.id = ar.department_id
            LEFT JOIN courses c ON c.id = ar.course_id
            ORDER BY ar.marked_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    elif user["role"] == "department_admin":
        rows = conn.execute(
            """
            SELECT ar.*, s.title AS session_title, s.session_date, s.session_type,
                   st.full_name AS student_name, st.matric_number,
                   d.name AS department_name, c.code AS course_code
            FROM attendance_records ar
            JOIN attendance_sessions s ON s.id = ar.session_id
            JOIN accounts st ON st.id = ar.student_id
            JOIN departments d ON d.id = ar.department_id
            LEFT JOIN courses c ON c.id = ar.course_id
            WHERE ar.department_id = ?
            ORDER BY ar.marked_at DESC
            LIMIT ?
            """,
            (user["department_id"], limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT ar.*, s.title AS session_title, s.session_date, s.session_type,
                   d.name AS department_name, c.code AS course_code
            FROM attendance_records ar
            JOIN attendance_sessions s ON s.id = ar.session_id
            JOIN departments d ON d.id = ar.department_id
            LEFT JOIN courses c ON c.id = ar.course_id
            WHERE ar.student_id = ?
            ORDER BY ar.marked_at DESC
            LIMIT ?
            """,
            (user["id"], limit),
        ).fetchall()
    conn.close()
    return rows


def get_student_performance(student_id: int) -> dict:
    student = get_account(student_id)
    if not student:
        return {"courses": [], "overall_percent": 0.0, "eligible_courses": 0, "general_summary": None}

    conn = get_conn()
    course_rows = conn.execute(
        """
        SELECT c.id, c.code, c.title, c.semester,
               COUNT(DISTINCT s.id) AS total_sessions,
               COUNT(DISTINCT ar.session_id) AS attended_sessions
        FROM courses c
        LEFT JOIN attendance_sessions s
               ON s.course_id = c.id
              AND s.department_id = c.department_id
              AND s.session_type = 'course'
        LEFT JOIN attendance_records ar
               ON ar.session_id = s.id
              AND ar.student_id = ?
        WHERE c.department_id = ?
        GROUP BY c.id, c.code, c.title, c.semester
        ORDER BY c.code, c.title
        """,
        (student_id, student["department_id"]),
    ).fetchall()

    general_row = conn.execute(
        """
        SELECT COUNT(DISTINCT s.id) AS total_sessions,
               COUNT(DISTINCT ar.session_id) AS attended_sessions
        FROM attendance_sessions s
        LEFT JOIN attendance_records ar
               ON ar.session_id = s.id
              AND ar.student_id = ?
        WHERE s.department_id = ? AND s.session_type = 'general'
        """,
        (student_id, student["department_id"]),
    ).fetchone()
    conn.close()

    courses = []
    total_attended = 0
    total_sessions = 0
    eligible_courses = 0
    for row in course_rows:
        attended = int(row["attended_sessions"] or 0)
        sessions_total = int(row["total_sessions"] or 0)
        percent = round((attended / sessions_total) * 100, 2) if sessions_total else 0.0
        eligible = percent >= 70 if sessions_total else False
        courses.append(
            {
                "id": row["id"],
                "code": row["code"],
                "title": row["title"],
                "semester": row["semester"],
                "attended": attended,
                "total_sessions": sessions_total,
                "attendance_percent": percent,
                "eligible": eligible,
            }
        )
        total_attended += attended
        total_sessions += sessions_total
        if eligible:
            eligible_courses += 1

    general_summary = None
    if general_row:
        attended = int(general_row["attended_sessions"] or 0)
        sessions_total = int(general_row["total_sessions"] or 0)
        percent = round((attended / sessions_total) * 100, 2) if sessions_total else 0.0
        general_summary = {
            "attended": attended,
            "total_sessions": sessions_total,
            "attendance_percent": percent,
            "eligible": percent >= 70 if sessions_total else False,
        }

    overall_percent = round((total_attended / total_sessions) * 100, 2) if total_sessions else 0.0
    return {
        "courses": courses,
        "overall_percent": overall_percent,
        "eligible_courses": eligible_courses,
        "general_summary": general_summary,
    }


def get_department_course_analytics(department_id: int) -> list[dict]:
    conn = get_conn()
    total_students_row = conn.execute(
        """
        SELECT COUNT(*) AS total_students
        FROM accounts
        WHERE role = 'student' AND department_id = ? AND is_active = 1
        """,
        (department_id,),
    ).fetchone()
    total_students = int(total_students_row["total_students"] or 0)

    course_rows = conn.execute(
        """
        SELECT c.id, c.code, c.title,
               COUNT(DISTINCT s.id) AS total_sessions,
               COUNT(DISTINCT ar.id) AS total_marks
        FROM courses c
        LEFT JOIN attendance_sessions s
               ON s.course_id = c.id
              AND s.session_type = 'course'
        LEFT JOIN attendance_records ar
               ON ar.session_id = s.id
        WHERE c.department_id = ?
        GROUP BY c.id, c.code, c.title
        ORDER BY c.code, c.title
        """,
        (department_id,),
    ).fetchall()

    analytics = []
    for course in course_rows:
        total_sessions = int(course["total_sessions"] or 0)
        total_marks = int(course["total_marks"] or 0)
        average_attendance = 0.0
        eligible_students = 0

        if total_students and total_sessions:
            average_attendance = round((total_marks / (total_students * total_sessions)) * 100, 2)
            per_student_rows = conn.execute(
                """
                SELECT st.id,
                       COUNT(DISTINCT ar.session_id) AS attended_sessions
                FROM accounts st
                LEFT JOIN attendance_records ar
                       ON ar.student_id = st.id
                      AND ar.course_id = ?
                WHERE st.role = 'student' AND st.department_id = ? AND st.is_active = 1
                GROUP BY st.id
                """,
                (course["id"], department_id),
            ).fetchall()
            for student_row in per_student_rows:
                percent = (int(student_row["attended_sessions"] or 0) / total_sessions) * 100
                if percent >= 70:
                    eligible_students += 1

        analytics.append(
            {
                "id": course["id"],
                "code": course["code"],
                "title": course["title"],
                "total_sessions": total_sessions,
                "average_attendance": average_attendance,
                "eligible_students": eligible_students,
                "total_students": total_students,
            }
        )

    conn.close()
    return analytics


def get_pending_reprint_requests(user: sqlite3.Row) -> list[sqlite3.Row]:
    conn = get_conn()
    if user["role"] == "super_admin":
        rows = conn.execute(
            """
            SELECT r.*, s.full_name AS student_name, s.matric_number, d.name AS department_name
            FROM id_card_requests r
            JOIN accounts s ON s.id = r.student_id
            JOIN departments d ON d.id = r.department_id
            ORDER BY CASE WHEN r.status = 'pending' THEN 0 ELSE 1 END, r.requested_at DESC
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT r.*, s.full_name AS student_name, s.matric_number, d.name AS department_name
            FROM id_card_requests r
            JOIN accounts s ON s.id = r.student_id
            JOIN departments d ON d.id = r.department_id
            WHERE r.department_id = ?
            ORDER BY CASE WHEN r.status = 'pending' THEN 0 ELSE 1 END, r.requested_at DESC
            """,
            (user["department_id"],),
        ).fetchall()
    conn.close()
    return rows


def get_flagged_content(user: sqlite3.Row, limit: int = 10) -> list[dict]:
    conn = get_conn()
    post_params: list = []
    comment_params: list = []
    post_department_clause = ""
    comment_department_clause = ""
    if user["role"] == "department_admin":
        post_department_clause = " AND p.department_id = ?"
        comment_department_clause = " AND c.department_id = ?"
        post_params.append(user["department_id"])
        comment_params.append(user["department_id"])

    posts = conn.execute(
        f"""
        SELECT 'post' AS item_type, p.id, p.department_id, p.title AS headline, p.body AS body,
               p.moderation_reason AS reason, p.created_at, a.full_name AS author_name,
               d.name AS department_name
        FROM community_posts p
        JOIN accounts a ON a.id = p.author_id
        JOIN departments d ON d.id = p.department_id
        WHERE p.moderation_status = 'flagged'{post_department_clause}
        ORDER BY p.created_at DESC
        LIMIT ?
        """,
        (*post_params, limit),
    ).fetchall()

    comments = conn.execute(
        f"""
        SELECT 'comment' AS item_type, c.id, c.department_id, p.title AS headline, c.body AS body,
               c.moderation_reason AS reason, c.created_at, a.full_name AS author_name,
               d.name AS department_name
        FROM community_comments c
        JOIN community_posts p ON p.id = c.post_id
        JOIN accounts a ON a.id = c.author_id
        JOIN departments d ON d.id = c.department_id
        WHERE c.moderation_status = 'flagged'{comment_department_clause}
        ORDER BY c.created_at DESC
        LIMIT ?
        """,
        (*comment_params, limit),
    ).fetchall()
    conn.close()

    items = [dict(row) for row in posts] + [dict(row) for row in comments]
    items.sort(key=lambda row: row["created_at"], reverse=True)
    return items[:limit]


def fetch_community_posts(user: sqlite3.Row, view_mode: str = "all") -> list[dict]:
    conn = get_conn()
    params: list = []
    visibility_condition = "1 = 1"

    if user["role"] in {"student", "department_admin"}:
        visibility_condition = "(p.visibility = 'all' OR p.department_id = ?)"
        params.append(user["department_id"])
        if view_mode == "department":
            visibility_condition = "p.department_id = ?"
    elif view_mode == "department" and user["department_id"]:
        visibility_condition = "p.department_id = ?"
        params.append(user["department_id"])

    posts = conn.execute(
        f"""
        SELECT p.*, a.full_name AS author_name, d.name AS department_name,
               (SELECT COUNT(*) FROM community_likes l WHERE l.post_id = p.id) AS likes_count,
               (SELECT COUNT(*) FROM community_comments c
                 WHERE c.post_id = p.id AND c.moderation_status IN ('clean', 'approved')) AS comments_count
        FROM community_posts p
        JOIN accounts a ON a.id = p.author_id
        JOIN departments d ON d.id = p.department_id
        WHERE p.moderation_status IN ('clean', 'approved')
          AND {visibility_condition}
        ORDER BY p.created_at DESC
        """,
        params,
    ).fetchall()

    post_ids = [row["id"] for row in posts]
    liked_ids = set()
    user_votes: dict[int, int] = {}
    comments_by_post: dict[int, list[dict]] = defaultdict(list)
    poll_options_by_post: dict[int, list[dict]] = defaultdict(list)

    if current_user() and current_user()["role"] == "student" and post_ids:
        placeholders = ",".join("?" for _ in post_ids)
        liked_rows = conn.execute(
            f"SELECT post_id FROM community_likes WHERE user_id = ? AND post_id IN ({placeholders})",
            (current_user()["id"], *post_ids),
        ).fetchall()
        liked_ids = {row["post_id"] for row in liked_rows}

        vote_rows = conn.execute(
            f"SELECT post_id, option_id FROM community_poll_votes WHERE voter_id = ? AND post_id IN ({placeholders})",
            (current_user()["id"], *post_ids),
        ).fetchall()
        user_votes = {row["post_id"]: row["option_id"] for row in vote_rows}

    if post_ids:
        placeholders = ",".join("?" for _ in post_ids)
        comment_rows = conn.execute(
            f"""
            SELECT c.*, a.full_name AS author_name
            FROM community_comments c
            JOIN accounts a ON a.id = c.author_id
            WHERE c.post_id IN ({placeholders})
              AND c.moderation_status IN ('clean', 'approved')
            ORDER BY c.created_at ASC
            """,
            post_ids,
        ).fetchall()
        for row in comment_rows:
            comments_by_post[row["post_id"]].append(dict(row))

        poll_rows = conn.execute(
            f"""
            SELECT o.id, o.post_id, o.option_text, COUNT(v.id) AS vote_count
            FROM community_poll_options o
            LEFT JOIN community_poll_votes v ON v.option_id = o.id
            WHERE o.post_id IN ({placeholders})
            GROUP BY o.id, o.post_id, o.option_text
            ORDER BY o.id ASC
            """,
            post_ids,
        ).fetchall()
        for row in poll_rows:
            poll_options_by_post[row["post_id"]].append(dict(row))

    conn.close()

    enriched_posts = []
    for row in posts:
        item = dict(row)
        options = poll_options_by_post.get(row["id"], [])
        total_votes = sum(int(option["vote_count"] or 0) for option in options)
        item["liked_by_user"] = row["id"] in liked_ids
        item["comments"] = comments_by_post.get(row["id"], [])
        item["poll_options"] = options
        item["user_vote_id"] = user_votes.get(row["id"])
        item["poll_total_votes"] = total_votes
        enriched_posts.append(item)
    return enriched_posts


def build_attendance_filters(user: sqlite3.Row) -> dict:
    raw_department = request.args.get("department_id")
    raw_course = request.args.get("course_id")
    filters = {
        "department_id": parse_department_for_request(user, raw_department),
        "course_id": int(raw_course) if raw_course else None,
        "session_type": normalize_text(request.args.get("session_type")),
        "date_from": normalize_text(request.args.get("date_from")),
        "date_to": normalize_text(request.args.get("date_to")),
        "student_query": normalize_text(request.args.get("student_query")),
    }
    if user["role"] != "super_admin":
        filters["department_id"] = user["department_id"]
    return filters


def fetch_attendance_records(user: sqlite3.Row, filters: dict) -> list[sqlite3.Row]:
    clauses = ["1 = 1"]
    params: list = []

    if filters.get("department_id"):
        clauses.append("ar.department_id = ?")
        params.append(filters["department_id"])
    if filters.get("course_id"):
        clauses.append("ar.course_id = ?")
        params.append(filters["course_id"])
    if filters.get("session_type"):
        clauses.append("s.session_type = ?")
        params.append(filters["session_type"])
    if filters.get("date_from"):
        clauses.append("s.session_date >= ?")
        params.append(filters["date_from"])
    if filters.get("date_to"):
        clauses.append("s.session_date <= ?")
        params.append(filters["date_to"])
    if filters.get("student_query"):
        like_value = f"%{filters['student_query']}%"
        clauses.append("(st.full_name LIKE ? OR st.matric_number LIKE ?)")
        params.extend([like_value, like_value])

    conn = get_conn()
    rows = conn.execute(
        f"""
        SELECT ar.id, ar.marked_at, ar.attendance_type,
               s.title AS session_title, s.session_type, s.session_date, s.start_time,
               st.full_name AS student_name, st.matric_number,
               d.name AS department_name, d.id AS department_id,
               c.id AS course_id, c.code AS course_code, c.title AS course_title
        FROM attendance_records ar
        JOIN attendance_sessions s ON s.id = ar.session_id
        JOIN accounts st ON st.id = ar.student_id
        JOIN departments d ON d.id = ar.department_id
        LEFT JOIN courses c ON c.id = ar.course_id
        WHERE {' AND '.join(clauses)}
        ORDER BY s.session_date DESC, ar.marked_at DESC
        """,
        params,
    ).fetchall()
    conn.close()
    return rows


def record_rows_for_export(records: list[sqlite3.Row]) -> list[list[str]]:
    export_rows = []
    for row in records:
        export_rows.append(
            [
                row["department_name"],
                row["student_name"],
                row["matric_number"],
                row["session_type"].title(),
                row["session_title"],
                row["course_code"] or "-",
                row["session_date"],
                row["start_time"],
                row["marked_at"],
            ]
        )
    return export_rows


def make_xlsx_bytes(sheet_name: str, headers: list[str], rows: list[list[object]]) -> io.BytesIO:
    def cell_ref(col_index: int, row_index: int) -> str:
        letters = ""
        current = col_index
        while current:
            current, remainder = divmod(current - 1, 26)
            letters = chr(65 + remainder) + letters
        return f"{letters}{row_index}"

    def xml_cell(col_index: int, row_index: int, value: object) -> str:
        reference = cell_ref(col_index, row_index)
        if value is None:
            return f'<c r="{reference}" t="inlineStr"><is><t></t></is></c>'
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f'<c r="{reference}"><v>{value}</v></c>'
        escaped = escape(str(value))
        return f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{escaped}</t></is></c>'

    all_rows = [headers] + rows
    sheet_rows_xml = []
    for row_index, values in enumerate(all_rows, start=1):
        cells = "".join(xml_cell(col_index, row_index, value) for col_index, value in enumerate(values, start=1))
        sheet_rows_xml.append(f'<row r="{row_index}">{cells}</row>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows_xml)}</sheetData>'
        "</worksheet>"
    )

    workbook_name = escape(sheet_name)
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "docProps/core.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
  xmlns:dc="http://purl.org/dc/elements/1.1/"
  xmlns:dcterms="http://purl.org/dc/terms/"
  xmlns:dcmitype="http://purl.org/dc/dcmitype/"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{escape(APP_NAME)} Attendance Export</dc:title>
  <dc:creator>{escape(APP_NAME)}</dc:creator>
  <cp:lastModifiedBy>{escape(APP_NAME)}</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified>
</cp:coreProperties>""",
        )
        archive.writestr(
            "docProps/app.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
  xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>OpenAI Codex</Application>
</Properties>""",
        )
        archive.writestr(
            "xl/workbook.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="{workbook_name}" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/styles.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="2">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
  </fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>""",
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    output.seek(0)
    return output


def render_attendance_pdf(records: list[sqlite3.Row], filters: dict) -> io.BytesIO:
    output = io.BytesIO()
    document = SimpleDocTemplate(output, pagesize=A4, rightMargin=25, leftMargin=25, topMargin=40, bottomMargin=30)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CampusTitle",
        parent=styles["Heading1"],
        fontSize=14,
        leading=18,
        alignment=1,
        spaceAfter=8,
        textColor=colors.HexColor("#1d3557"),
    )
    info_style = ParagraphStyle(
        "CampusInfo",
        parent=styles["BodyText"],
        fontSize=9,
        leading=12,
        alignment=1,
        textColor=colors.HexColor("#34495e"),
    )

    elements = []
    if os.path.exists(LOGO_PATH):
        elements.append(Image(LOGO_PATH, width=60, height=60))
        elements.append(Spacer(1, 6))

    elements.append(Paragraph("Kaduna State University", title_style))
    elements.append(Paragraph("Smart Campus Attendance Report", title_style))

    filter_bits = []
    if filters.get("department_id"):
        department = get_department(filters["department_id"])
        if department:
            filter_bits.append(f"Department: {department['name']}")
    if filters.get("course_id"):
        conn = get_conn()
        course = conn.execute("SELECT code, title FROM courses WHERE id = ?", (filters["course_id"],)).fetchone()
        conn.close()
        if course:
            filter_bits.append(f"Course: {course['code']} - {course['title']}")
    if filters.get("session_type"):
        filter_bits.append(f"Type: {filters['session_type'].title()}")
    if filters.get("date_from"):
        filter_bits.append(f"From: {filters['date_from']}")
    if filters.get("date_to"):
        filter_bits.append(f"To: {filters['date_to']}")
    if not filter_bits:
        filter_bits.append("Scope: All available records")

    elements.append(Paragraph(" | ".join(filter_bits), info_style))
    elements.append(Spacer(1, 12))

    table_data = [[
        "Department",
        "Student",
        "Matric",
        "Type",
        "Session",
        "Course",
        "Date",
        "Marked At",
    ]]
    for row in records:
        table_data.append(
            [
                row["department_name"],
                row["student_name"],
                row["matric_number"],
                row["session_type"].title(),
                row["session_title"],
                row["course_code"] or "-",
                row["session_date"],
                row["marked_at"],
            ]
        )

    table = Table(table_data, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1d3557")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c7d3dd")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef4f8")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    elements.append(table)
    document.build(elements)
    output.seek(0)
    return output


def resolve_student_from_scan(scan_value: str) -> sqlite3.Row | None:
    raw = normalize_text(scan_value)
    if not raw:
        return None

    conn = get_conn()
    student = None
    if raw.startswith("SCMS:"):
        student = conn.execute(
            """
            SELECT a.*, d.name AS department_name
            FROM accounts a
            LEFT JOIN departments d ON d.id = a.department_id
            WHERE a.qr_token = ? AND a.role = 'student'
            LIMIT 1
            """,
            (raw,),
        ).fetchone()
    elif "|" in raw:
        matric = normalize_matric(raw.split("|", 1)[0])
        student = conn.execute(
            """
            SELECT a.*, d.name AS department_name
            FROM accounts a
            LEFT JOIN departments d ON d.id = a.department_id
            WHERE a.matric_number = ? AND a.role = 'student'
            LIMIT 1
            """,
            (matric,),
        ).fetchone()
    else:
        matric = normalize_matric(raw)
        student = conn.execute(
            """
            SELECT a.*, d.name AS department_name
            FROM accounts a
            LEFT JOIN departments d ON d.id = a.department_id
            WHERE (a.qr_token = ? OR a.matric_number = ?) AND a.role = 'student'
            LIMIT 1
            """,
            (raw, matric),
        ).fetchone()
    conn.close()
    return student


def student_records_for_dashboard(user: sqlite3.Row) -> dict:
    performance = get_student_performance(user["id"])
    conn = get_conn()
    pending_reprint = conn.execute(
        """
        SELECT COUNT(*) AS pending_count
        FROM id_card_requests
        WHERE student_id = ? AND status = 'pending'
        """,
        (user["id"],),
    ).fetchone()
    conn.close()

    student = ensure_student_qr(user["id"])
    return {
        "performance": performance,
        "recent_attendance": get_recent_attendance(user, limit=10),
        "pending_reprint_count": int(pending_reprint["pending_count"] or 0),
        "student": student,
    }


def admin_dashboard_data(user: sqlite3.Row) -> dict:
    conn = get_conn()
    if user["role"] == "super_admin":
        counts = {
            "departments": int(conn.execute("SELECT COUNT(*) AS count FROM departments").fetchone()["count"] or 0),
            "department_admins": int(conn.execute("SELECT COUNT(*) AS count FROM accounts WHERE role = 'department_admin'").fetchone()["count"] or 0),
            "students": int(conn.execute("SELECT COUNT(*) AS count FROM accounts WHERE role = 'student'").fetchone()["count"] or 0),
            "courses": int(conn.execute("SELECT COUNT(*) AS count FROM courses").fetchone()["count"] or 0),
            "active_sessions": int(conn.execute("SELECT COUNT(*) AS count FROM attendance_sessions WHERE status = 'active'").fetchone()["count"] or 0),
        }
        departments = conn.execute(
            """
            SELECT d.*,
                   (SELECT COUNT(*) FROM accounts a WHERE a.department_id = d.id AND a.role = 'student') AS student_count,
                   (SELECT COUNT(*) FROM accounts a WHERE a.department_id = d.id AND a.role = 'department_admin') AS admin_count,
                   (SELECT COUNT(*) FROM courses c WHERE c.department_id = d.id) AS course_count
            FROM departments d
            ORDER BY d.name
            """
        ).fetchall()
        department_admins = conn.execute(
            """
            SELECT a.*, d.name AS department_name
            FROM accounts a
            LEFT JOIN departments d ON d.id = a.department_id
            WHERE a.role = 'department_admin'
            ORDER BY d.name, a.full_name
            """
        ).fetchall()
        students = conn.execute(
            """
            SELECT a.*, d.name AS department_name
            FROM accounts a
            JOIN departments d ON d.id = a.department_id
            WHERE a.role = 'student'
            ORDER BY a.created_at DESC
            LIMIT 12
            """
        ).fetchall()
    else:
        counts = {
            "departments": 1,
            "department_admins": 1,
            "students": int(conn.execute("SELECT COUNT(*) AS count FROM accounts WHERE role = 'student' AND department_id = ?", (user["department_id"],)).fetchone()["count"] or 0),
            "courses": int(conn.execute("SELECT COUNT(*) AS count FROM courses WHERE department_id = ?", (user["department_id"],)).fetchone()["count"] or 0),
            "active_sessions": int(conn.execute("SELECT COUNT(*) AS count FROM attendance_sessions WHERE status = 'active' AND department_id = ?", (user["department_id"],)).fetchone()["count"] or 0),
        }
        departments = conn.execute("SELECT * FROM departments WHERE id = ?", (user["department_id"],)).fetchall()
        department_admins = [user]
        students = conn.execute(
            """
            SELECT a.*, d.name AS department_name
            FROM accounts a
            JOIN departments d ON d.id = a.department_id
            WHERE a.role = 'student' AND a.department_id = ?
            ORDER BY a.created_at DESC
            LIMIT 15
            """,
            (user["department_id"],),
        ).fetchall()
    conn.close()

    analytics = []
    departments_scope = get_accessible_departments_for_user(user)
    for department in departments_scope:
        if department:
            analytics.extend(get_department_course_analytics(department["id"]))

    return {
        "counts": counts,
        "departments": departments,
        "department_admins": department_admins,
        "students": students,
        "courses": get_courses_for_department(user["department_id"]) if user["role"] == "department_admin" else [],
        "recent_sessions": get_recent_sessions(user),
        "recent_attendance": get_recent_attendance(user),
        "reprint_requests": get_pending_reprint_requests(user),
        "flagged_items": get_flagged_content(user),
        "analytics": analytics[:10],
    }


@app.route("/", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("home"))

    if request.method == "POST":
        identifier = request.form.get("identifier")
        password = normalize_text(request.form.get("password"))
        account = get_account_by_identifier(identifier)
        if not account or not check_password_hash(account["password"], password):
            flash("Invalid login details.", "error")
            return render_template("login.html")
        if not account["is_active"]:
            flash("This account has been deactivated. Please contact an administrator.", "error")
            return render_template("login.html")
        login_user(account)
        return redirect(url_for("home"))

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    departments = get_departments()
    if request.method == "POST":
        full_name = normalize_text(request.form.get("full_name"))
        matric_number = normalize_matric(request.form.get("matric_number"))
        email = normalize_email(request.form.get("email"))
        phone = normalize_text(request.form.get("phone"))
        password = normalize_text(request.form.get("password"))
        confirm_password = normalize_text(request.form.get("confirm_password"))
        department_id = request.form.get("department_id")
        profile_photo = request.files.get("profile_photo")

        if not departments:
            flash("Registration will open after the super admin creates at least one department.", "error")
            return render_template("register.html", departments=departments)
        if not all([full_name, matric_number, email, phone, password, confirm_password, department_id]):
            flash("Please complete every required field.", "error")
            return render_template("register.html", departments=departments)
        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("register.html", departments=departments)
        if not profile_photo or not profile_photo.filename:
            flash("A profile photo is required.", "error")
            return render_template("register.html", departments=departments)

        try:
            saved_photo = save_upload(profile_photo, os.path.join("uploads", "profiles"), "profile", IMAGE_EXTENSIONS)
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("register.html", departments=departments)

        conn = get_conn()
        existing = conn.execute(
            """
            SELECT id FROM accounts
            WHERE LOWER(email) = ? OR UPPER(COALESCE(matric_number, '')) = ?
            LIMIT 1
            """,
            (email, matric_number),
        ).fetchone()
        if existing:
            conn.close()
            flash("A student with this email or matric number already exists.", "error")
            return render_template("register.html", departments=departments)

        department = get_department(int(department_id))
        if not department:
            conn.close()
            flash("Selected department was not found.", "error")
            return render_template("register.html", departments=departments)

        created_at = now_iso()
        cursor = conn.execute(
            """
            INSERT INTO accounts (
                username, password, role, full_name, email, phone,
                department_id, matric_number, profile_photo, created_at, updated_at
            )
            VALUES (?, ?, 'student', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalize_username(matric_number),
                generate_password_hash(password),
                full_name,
                email,
                phone,
                int(department_id),
                matric_number,
                saved_photo,
                created_at,
                created_at,
            ),
        )
        student_id = cursor.lastrowid
        conn.commit()
        conn.close()
        ensure_student_qr(student_id)

        flash("Registration successful. You can now log in with your matric number or email.", "success")
        return redirect(url_for("login"))

    return render_template("register.html", departments=departments)


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/change_password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        user = current_user()
        current_password = normalize_text(request.form.get("current_password"))
        new_password = normalize_text(request.form.get("new_password"))
        confirm_password = normalize_text(request.form.get("confirm_password"))

        if not check_password_hash(user["password"], current_password):
            flash("Current password is incorrect.", "error")
        elif not new_password:
            flash("New password cannot be empty.", "error")
        elif new_password != confirm_password:
            flash("New password and confirmation do not match.", "error")
        else:
            conn = get_conn()
            conn.execute(
                "UPDATE accounts SET password = ?, updated_at = ? WHERE id = ?",
                (generate_password_hash(new_password), now_iso(), user["id"]),
            )
            conn.commit()
            conn.close()
            flash("Password updated successfully.", "success")
            return redirect(url_for("home"))

    return render_template("change_password.html")


@app.route("/home")
@login_required
def home():
    user = current_user()
    departments = get_accessible_departments_for_user(user)
    dashboard = student_records_for_dashboard(user) if user["role"] == "student" else admin_dashboard_data(user)

    course_lookup = {}
    for department in departments:
        if department:
            course_lookup[department["id"]] = get_courses_for_department(department["id"])

    return render_template(
        "index.html",
        dashboard=dashboard,
        departments=departments,
        course_lookup=course_lookup,
    )


@app.route("/generate")
def generate():
    return redirect(url_for("register"))


@app.route("/profile/update", methods=["POST"])
@login_required
def update_profile():
    user = current_user()
    full_name = normalize_text(request.form.get("full_name"))
    email = normalize_email(request.form.get("email"))
    phone = normalize_text(request.form.get("phone"))
    profile_photo = request.files.get("profile_photo")

    if not all([full_name, email, phone]):
        flash("Full name, email, and phone are required.", "error")
        return redirect(url_for("home"))

    conn = get_conn()
    existing = conn.execute(
        "SELECT id FROM accounts WHERE LOWER(email) = ? AND id != ? LIMIT 1",
        (email, user["id"]),
    ).fetchone()
    if existing:
        conn.close()
        flash("Another account already uses that email address.", "error")
        return redirect(url_for("home"))

    profile_path = user["profile_photo"]
    if profile_photo and profile_photo.filename:
        try:
            profile_path = save_upload(profile_photo, os.path.join("uploads", "profiles"), "profile", IMAGE_EXTENSIONS)
        except ValueError as exc:
            conn.close()
            flash(str(exc), "error")
            return redirect(url_for("home"))

    conn.execute(
        """
        UPDATE accounts
        SET full_name = ?, email = ?, phone = ?, profile_photo = ?, updated_at = ?
        WHERE id = ?
        """,
        (full_name, email, phone, profile_path, now_iso(), user["id"]),
    )
    conn.commit()
    conn.close()
    flash("Profile updated.", "success")
    return redirect(url_for("home"))


@app.route("/departments/create", methods=["POST"])
@roles_required("super_admin")
def create_department():
    name = normalize_text(request.form.get("name"))
    code = normalize_department_code(request.form.get("code"))
    description = normalize_text(request.form.get("description"))

    if not name or not code:
        flash("Department name and code are required.", "error")
        return redirect(url_for("home"))

    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO departments (name, code, description, created_at) VALUES (?, ?, ?, ?)",
            (name, code, description, now_iso()),
        )
        conn.commit()
        flash("Department created.", "success")
    except sqlite3.IntegrityError:
        flash("Department name or code already exists.", "error")
    finally:
        conn.close()
    return redirect(url_for("home"))


@app.route("/departments/<int:department_id>/delete", methods=["POST"])
@roles_required("super_admin")
def delete_department(department_id: int):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM departments WHERE id = ?", (department_id,))
        conn.commit()
        flash("Department deleted.", "success")
    except sqlite3.IntegrityError:
        flash("Department cannot be deleted while it still has linked records.", "error")
    finally:
        conn.close()
    return redirect(url_for("home"))


@app.route("/admins/create", methods=["POST"])
@roles_required("super_admin")
def create_department_admin():
    full_name = normalize_text(request.form.get("full_name"))
    username = normalize_username(request.form.get("username"))
    email = normalize_email(request.form.get("email"))
    phone = normalize_text(request.form.get("phone"))
    password = normalize_text(request.form.get("password"))
    department_id = request.form.get("department_id")

    if not all([full_name, username, email, phone, password, department_id]):
        flash("All department admin fields are required.", "error")
        return redirect(url_for("home"))

    department = get_department(int(department_id))
    if not department:
        flash("Selected department was not found.", "error")
        return redirect(url_for("home"))

    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO accounts (
                username, password, role, full_name, email, phone,
                department_id, created_at, updated_at
            )
            VALUES (?, ?, 'department_admin', ?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                generate_password_hash(password),
                full_name,
                email,
                phone,
                int(department_id),
                now_iso(),
                now_iso(),
            ),
        )
        conn.commit()
        flash("Department admin created.", "success")
    except sqlite3.IntegrityError:
        flash("Username or email already exists.", "error")
    finally:
        conn.close()
    return redirect(url_for("home"))


@app.route("/accounts/<int:account_id>/toggle", methods=["POST"])
@roles_required("super_admin", "department_admin")
def toggle_account_status(account_id: int):
    user = current_user()
    target = get_account(account_id)
    if not target:
        abort(404)
    if target["role"] == "super_admin":
        flash("Super admin accounts cannot be deactivated here.", "error")
        return redirect(url_for("home"))
    if user["role"] == "department_admin":
        if target["role"] != "student":
            abort(403)
        require_department_access(user, target["department_id"])

    conn = get_conn()
    conn.execute(
        "UPDATE accounts SET is_active = ?, updated_at = ? WHERE id = ?",
        (0 if target["is_active"] else 1, now_iso(), account_id),
    )
    conn.commit()
    conn.close()
    flash("Account status updated.", "success")
    return redirect(url_for("home"))


@app.route("/accounts/<int:account_id>/delete", methods=["POST"])
@roles_required("super_admin", "department_admin")
def delete_account(account_id: int):
    user = current_user()
    target = get_account(account_id)
    if not target:
        abort(404)
    if target["role"] == "super_admin":
        flash("The main super admin account cannot be deleted.", "error")
        return redirect(url_for("home"))
    if user["role"] == "department_admin":
        if target["role"] != "student":
            abort(403)
        require_department_access(user, target["department_id"])

    conn = get_conn()
    try:
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.commit()
        flash("Account deleted.", "success")
    except sqlite3.IntegrityError:
        flash("Account cannot be deleted while linked records still exist. Deactivate it instead.", "error")
    finally:
        conn.close()
    return redirect(url_for("home"))


@app.route("/courses/create", methods=["POST"])
@roles_required("super_admin", "department_admin")
def create_course():
    user = current_user()
    department_id = parse_department_for_request(user, request.form.get("department_id"))
    code = normalize_text(request.form.get("code")).upper()
    title = normalize_text(request.form.get("title"))
    semester = normalize_text(request.form.get("semester"))

    if not department_id or not code or not title:
        flash("Department, course code, and title are required.", "error")
        return redirect(url_for("home"))

    require_department_access(user, department_id)
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO courses (department_id, code, title, semester, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (department_id, code, title, semester, user["id"], now_iso()),
        )
        conn.commit()
        flash("Course created.", "success")
    except sqlite3.IntegrityError:
        flash("That course code already exists in the selected department.", "error")
    finally:
        conn.close()
    return redirect(url_for("home"))


@app.route("/courses/<int:course_id>/delete", methods=["POST"])
@roles_required("super_admin", "department_admin")
def delete_course(course_id: int):
    user = current_user()
    conn = get_conn()
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    if not course:
        conn.close()
        abort(404)
    require_department_access(user, course["department_id"])
    try:
        conn.execute("DELETE FROM courses WHERE id = ?", (course_id,))
        conn.commit()
        flash("Course deleted.", "success")
    except sqlite3.IntegrityError:
        flash("Course cannot be deleted while attendance sessions still reference it.", "error")
    finally:
        conn.close()
    return redirect(url_for("home"))


@app.route("/scan")
@roles_required("super_admin", "department_admin")
def scan():
    user = current_user()
    selected_department_id = parse_department_for_request(user, request.args.get("department_id"))
    if not selected_department_id:
        departments = get_accessible_departments_for_user(user)
        if departments:
            selected_department_id = departments[0]["id"]
    require_department_access(user, selected_department_id)

    active_session = get_active_session_for_department(selected_department_id)
    departments = get_accessible_departments_for_user(user)
    return render_template(
        "scan.html",
        selected_department_id=selected_department_id,
        departments=departments,
        courses=get_courses_for_department(selected_department_id),
        active_session=active_session,
        recent_sessions=get_recent_sessions(user, limit=8),
    )


@app.route("/sessions/start", methods=["POST"])
@roles_required("super_admin", "department_admin")
def start_session():
    user = current_user()
    department_id = parse_department_for_request(user, request.form.get("department_id"))
    session_type = normalize_text(request.form.get("session_type")) or "general"
    title = normalize_text(request.form.get("title"))
    course_id = request.form.get("course_id")

    if not department_id:
        flash("Please choose a department.", "error")
        return redirect(url_for("scan"))
    require_department_access(user, department_id)

    course_row = None
    if session_type == "course":
        if not course_id:
            flash("Course-specific sessions require a course.", "error")
            return redirect(url_for("scan", department_id=department_id))
        conn = get_conn()
        course_row = conn.execute(
            "SELECT * FROM courses WHERE id = ? AND department_id = ?",
            (int(course_id), department_id),
        ).fetchone()
        conn.close()
        if not course_row:
            flash("Selected course was not found in that department.", "error")
            return redirect(url_for("scan", department_id=department_id))
        if not title:
            title = f"{course_row['code']} Attendance"
    else:
        course_id = None
        if not title:
            title = "General Attendance"

    now = now_local()
    conn = get_conn()
    conn.execute(
        """
        UPDATE attendance_sessions
        SET status = 'closed', closed_at = ?
        WHERE department_id = ? AND status = 'active'
        """,
        (now.isoformat(timespec="seconds"), department_id),
    )
    conn.execute(
        """
        INSERT INTO attendance_sessions (
            department_id, course_id, session_type, title, session_date,
            start_time, started_by, status, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (
            department_id,
            int(course_id) if course_id else None,
            session_type,
            title,
            now.strftime("%Y-%m-%d"),
            now.strftime("%H:%M:%S"),
            user["id"],
            now.isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    conn.close()
    flash("Attendance session started.", "success")
    return redirect(url_for("scan", department_id=department_id))


@app.route("/sessions/<int:session_id>/close", methods=["POST"])
@roles_required("super_admin", "department_admin")
def close_session(session_id: int):
    user = current_user()
    conn = get_conn()
    session_row = conn.execute(
        "SELECT * FROM attendance_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if not session_row:
        conn.close()
        abort(404)
    require_department_access(user, session_row["department_id"])
    conn.execute(
        "UPDATE attendance_sessions SET status = 'closed', closed_at = ? WHERE id = ?",
        (now_iso(), session_id),
    )
    conn.commit()
    conn.close()
    flash("Attendance session closed.", "success")
    return redirect(url_for("scan", department_id=session_row["department_id"]))


@app.route("/mark_attendance", methods=["POST"])
@roles_required("super_admin", "department_admin")
def mark_attendance():
    user = current_user()
    payload = request.get_json(silent=True) or {}
    scan_value = normalize_text(payload.get("data") or payload.get("token"))
    session_id = payload.get("session_id")
    department_id = payload.get("department_id")

    if not session_id:
        return jsonify({"success": False, "message": "No active attendance session was selected."}), 400

    conn = get_conn()
    session_row = conn.execute(
        """
        SELECT s.*, c.code AS course_code, c.title AS course_title
        FROM attendance_sessions s
        LEFT JOIN courses c ON c.id = s.course_id
        WHERE s.id = ?
        """,
        (int(session_id),),
    ).fetchone()
    conn.close()

    if not session_row:
        return jsonify({"success": False, "message": "Attendance session not found."}), 404
    if session_row["status"] != "active":
        return jsonify({"success": False, "message": "This attendance session is already closed."}), 400
    if department_id and int(department_id) != session_row["department_id"]:
        return jsonify({"success": False, "message": "Department mismatch for this session."}), 400

    require_department_access(user, session_row["department_id"])
    student = resolve_student_from_scan(scan_value)
    if not student or student["role"] != "student":
        return jsonify({"success": False, "message": "The scanned QR code is not linked to a valid student."}), 404
    if not student["is_active"]:
        return jsonify({"success": False, "message": "This student account is currently inactive."}), 400
    if student["department_id"] != session_row["department_id"]:
        return jsonify({"success": False, "message": "This student does not belong to the active department session."}), 400

    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO attendance_records (
                session_id, student_id, course_id, department_id, attendance_type, marked_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_row["id"],
                student["id"],
                session_row["course_id"],
                session_row["department_id"],
                session_row["session_type"],
                now_iso(),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify(
            {
                "success": False,
                "message": f"{student['full_name']} has already been marked for this session.",
            }
        ), 409
    conn.close()

    course_label = session_row["course_code"] or "General"
    return jsonify(
        {
            "success": True,
            "message": f"Attendance recorded for {student['full_name']} ({student['matric_number']}) in {course_label}.",
            "student": {
                "name": student["full_name"],
                "matric_number": student["matric_number"],
            },
        }
    )


@app.route("/history")
@roles_required("super_admin", "department_admin")
def history():
    user = current_user()
    filters = build_attendance_filters(user)
    if filters.get("department_id"):
        require_department_access(user, filters["department_id"])
    records = fetch_attendance_records(user, filters)
    departments = get_accessible_departments_for_user(user)
    courses = get_courses_for_department(filters["department_id"]) if filters.get("department_id") else []
    return render_template(
        "history.html",
        records=records,
        filters=filters,
        departments=departments,
        courses=courses,
    )


@app.route("/attendance/<int:record_id>/delete", methods=["POST"])
@roles_required("super_admin", "department_admin")
def delete_attendance_record(record_id: int):
    user = current_user()
    conn = get_conn()
    record = conn.execute(
        "SELECT id, department_id FROM attendance_records WHERE id = ?",
        (record_id,),
    ).fetchone()
    if not record:
        conn.close()
        abort(404)
    require_department_access(user, record["department_id"])
    conn.execute("DELETE FROM attendance_records WHERE id = ?", (record_id,))
    conn.commit()
    conn.close()
    flash("Attendance record deleted.", "success")
    return redirect(url_for("history", **request.args))


@app.route("/export_pdf", methods=["GET", "POST"])
@roles_required("super_admin", "department_admin")
def export_pdf():
    user = current_user()
    filters = build_attendance_filters(user)
    if filters.get("department_id"):
        require_department_access(user, filters["department_id"])
    records = fetch_attendance_records(user, filters)
    if not records:
        flash("No attendance records matched the selected filters.", "error")
        return redirect(url_for("history", **request.args))
    pdf_bytes = render_attendance_pdf(records, filters)
    return send_file(
        pdf_bytes,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="attendance_report.pdf",
    )


@app.route("/download_by_date")
@roles_required("super_admin", "department_admin")
def download_by_date():
    user = current_user()
    filters = build_attendance_filters(user)
    if filters.get("department_id"):
        require_department_access(user, filters["department_id"])
    records = fetch_attendance_records(user, filters)
    if not records:
        flash("No attendance records matched the selected filters.", "error")
        return redirect(url_for("history", **request.args))

    headers = [
        "Department",
        "Student Name",
        "Matric Number",
        "Attendance Type",
        "Session Title",
        "Course Code",
        "Session Date",
        "Session Start Time",
        "Marked At",
    ]
    xlsx_bytes = make_xlsx_bytes("Attendance", headers, record_rows_for_export(records))
    return send_file(
        xlsx_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="attendance_report.xlsx",
    )


@app.route("/my_attendance")
@roles_required("student")
def my_attendance():
    user = current_user()
    performance = get_student_performance(user["id"])
    records = get_recent_attendance(user, limit=100)
    return render_template(
        "my_attendance.html",
        performance=performance,
        records=records,
        student=ensure_student_qr(user["id"]),
    )


@app.route("/id-card")
@login_required
def id_card():
    user = current_user()
    if user["role"] == "student":
        student = ensure_student_qr(user["id"])
        conn = get_conn()
        requests_rows = conn.execute(
            """
            SELECT r.*, reviewer.full_name AS reviewer_name
            FROM id_card_requests r
            LEFT JOIN accounts reviewer ON reviewer.id = r.reviewed_by
            WHERE r.student_id = ?
            ORDER BY r.requested_at DESC
            """,
            (user["id"],),
        ).fetchall()
        conn.close()
        return render_template("id_card.html", student=student, requests_rows=requests_rows)

    return render_template("id_card.html", requests_rows=get_pending_reprint_requests(user), student=None)


@app.route("/id-card/generate", methods=["POST"])
@roles_required("student")
def generate_id_card():
    user = ensure_student_qr(current_user()["id"])
    conn = get_conn()
    generated_at = user["id_card_generated_at"] or now_iso()
    conn.execute(
        "UPDATE accounts SET id_card_generated_at = ?, updated_at = ? WHERE id = ?",
        (generated_at, now_iso(), user["id"]),
    )
    conn.commit()
    conn.close()
    flash("Your digital ID card is ready.", "success")
    return redirect(url_for("id_card"))


@app.route("/id-card/request-reprint", methods=["POST"])
@roles_required("student")
def request_id_reprint():
    user = ensure_student_qr(current_user()["id"])
    if not user["id_card_generated_at"]:
        flash("Generate your first ID card before requesting a reprint.", "error")
        return redirect(url_for("id_card"))

    receipt = request.files.get("receipt")
    note = normalize_text(request.form.get("note"))
    if not receipt or not receipt.filename:
        flash("Please upload a payment receipt for the reprint request.", "error")
        return redirect(url_for("id_card"))

    try:
        receipt_path = save_upload(receipt, os.path.join("uploads", "receipts"), "receipt", RECEIPT_EXTENSIONS)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("id_card"))

    conn = get_conn()
    pending = conn.execute(
        """
        SELECT id FROM id_card_requests
        WHERE student_id = ? AND status = 'pending'
        LIMIT 1
        """,
        (user["id"],),
    ).fetchone()
    if pending:
        conn.close()
        flash("You already have a pending reprint request.", "error")
        return redirect(url_for("id_card"))

    conn.execute(
        """
        INSERT INTO id_card_requests (
            student_id, department_id, receipt_path, amount, status, note, requested_at
        )
        VALUES (?, ?, ?, 1000, 'pending', ?, ?)
        """,
        (user["id"], user["department_id"], receipt_path, note, now_iso()),
    )
    conn.commit()
    conn.close()
    flash("Reprint request submitted for department approval.", "success")
    return redirect(url_for("id_card"))


@app.route("/id-card/requests/<int:request_id>/review", methods=["POST"])
@roles_required("super_admin", "department_admin")
def review_id_request(request_id: int):
    user = current_user()
    decision = normalize_text(request.form.get("decision"))
    note = normalize_text(request.form.get("note"))

    conn = get_conn()
    request_row = conn.execute(
        "SELECT * FROM id_card_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    if not request_row:
        conn.close()
        abort(404)
    require_department_access(user, request_row["department_id"])
    if decision not in {"approved", "rejected"}:
        conn.close()
        flash("Invalid reprint review action.", "error")
        return redirect(url_for("id_card"))

    conn.execute(
        """
        UPDATE id_card_requests
        SET status = ?, note = ?, reviewed_at = ?, reviewed_by = ?
        WHERE id = ?
        """,
        (decision, note or request_row["note"], now_iso(), user["id"], request_id),
    )
    if decision == "approved":
        conn.execute(
            """
            UPDATE accounts
            SET id_card_reprint_count = id_card_reprint_count + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (now_iso(), request_row["student_id"]),
        )
    conn.commit()
    conn.close()
    flash("ID card request reviewed.", "success")
    return redirect(url_for("id_card"))


@app.route("/id-card/print/<int:student_id>")
@login_required
def print_id_card(student_id: int):
    student = require_student_owner_or_admin(student_id)
    student = ensure_student_qr(student["id"])
    if not student["id_card_generated_at"] and current_user()["role"] == "student":
        flash("Generate your ID card first.", "error")
        return redirect(url_for("id_card"))
    return render_template("id_card_print.html", student=student)


@app.route("/community")
@login_required
def community():
    user = current_user()
    view_mode = normalize_text(request.args.get("view", "all")) or "all"
    posts = fetch_community_posts(user, view_mode=view_mode)
    flagged_items = get_flagged_content(user) if user["role"] in {"super_admin", "department_admin"} else []
    return render_template(
        "community.html",
        posts=posts,
        view_mode=view_mode,
        departments=get_accessible_departments_for_user(user),
        flagged_items=flagged_items,
    )


@app.route("/community/post", methods=["POST"])
@roles_required("student")
def create_community_post():
    user = current_user()
    post_type = normalize_text(request.form.get("post_type")) or "post"
    visibility = normalize_text(request.form.get("visibility")) or "department"
    title = normalize_text(request.form.get("title"))
    body = normalize_text(request.form.get("body"))
    poll_options_raw = normalize_text(request.form.get("poll_options"))

    if not title or not body:
        flash("Posts need both a title and a body.", "error")
        return redirect(url_for("community"))
    if visibility not in {"department", "all"}:
        flash("Invalid post visibility.", "error")
        return redirect(url_for("community"))
    if post_type not in {"post", "poll"}:
        flash("Invalid post type.", "error")
        return redirect(url_for("community"))

    poll_options = [normalize_text(line) for line in poll_options_raw.splitlines() if normalize_text(line)]
    if post_type == "poll" and len(poll_options) < 2:
        flash("Polls need at least two options.", "error")
        return redirect(url_for("community"))

    moderation_status, moderation_reason = moderate_text(title, body, poll_options_raw)
    conn = get_conn()
    cursor = conn.execute(
        """
        INSERT INTO community_posts (
            author_id, department_id, post_type, visibility, title, body,
            moderation_status, moderation_reason, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["id"],
            user["department_id"],
            post_type,
            visibility,
            title,
            body,
            moderation_status,
            moderation_reason,
            now_iso(),
        ),
    )
    post_id = cursor.lastrowid
    for option in poll_options:
        conn.execute(
            """
            INSERT INTO community_poll_options (post_id, department_id, option_text)
            VALUES (?, ?, ?)
            """,
            (post_id, user["department_id"], option),
        )
    conn.commit()
    conn.close()

    if moderation_status == "flagged":
        flash("Your post was saved and flagged for admin review before it becomes visible.", "info")
    else:
        flash("Post published to the student community.", "success")
    return redirect(url_for("community"))


@app.route("/community/posts/<int:post_id>/like", methods=["POST"])
@roles_required("student")
def toggle_like(post_id: int):
    user = current_user()
    conn = get_conn()
    post = conn.execute("SELECT id FROM community_posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        conn.close()
        abort(404)
    existing = conn.execute(
        "SELECT id FROM community_likes WHERE post_id = ? AND user_id = ?",
        (post_id, user["id"]),
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM community_likes WHERE id = ?", (existing["id"],))
    else:
        conn.execute(
            """
            INSERT INTO community_likes (post_id, user_id, department_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (post_id, user["id"], user["department_id"], now_iso()),
        )
    conn.commit()
    conn.close()
    return redirect(url_for("community"))


@app.route("/community/posts/<int:post_id>/comment", methods=["POST"])
@roles_required("student")
def add_comment(post_id: int):
    user = current_user()
    body = normalize_text(request.form.get("body"))
    if not body:
        flash("Comments cannot be empty.", "error")
        return redirect(url_for("community"))

    conn = get_conn()
    post = conn.execute("SELECT id FROM community_posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        conn.close()
        abort(404)
    moderation_status, moderation_reason = moderate_text(body)
    conn.execute(
        """
        INSERT INTO community_comments (
            post_id, author_id, department_id, body,
            moderation_status, moderation_reason, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            post_id,
            user["id"],
            user["department_id"],
            body,
            moderation_status,
            moderation_reason,
            now_iso(),
        ),
    )
    conn.commit()
    conn.close()

    if moderation_status == "flagged":
        flash("Your comment was flagged for admin review.", "info")
    else:
        flash("Comment added.", "success")
    return redirect(url_for("community"))


@app.route("/community/posts/<int:post_id>/vote", methods=["POST"])
@roles_required("student")
def vote_poll(post_id: int):
    user = current_user()
    option_id = request.form.get("option_id")
    if not option_id:
        flash("Choose an option before voting.", "error")
        return redirect(url_for("community"))

    conn = get_conn()
    option = conn.execute(
        """
        SELECT o.id
        FROM community_poll_options o
        JOIN community_posts p ON p.id = o.post_id
        WHERE o.id = ? AND o.post_id = ?
        """,
        (int(option_id), post_id),
    ).fetchone()
    if not option:
        conn.close()
        abort(404)

    existing_vote = conn.execute(
        "SELECT id FROM community_poll_votes WHERE post_id = ? AND voter_id = ?",
        (post_id, user["id"]),
    ).fetchone()
    if existing_vote:
        conn.close()
        flash("You have already voted in this poll.", "error")
        return redirect(url_for("community"))

    conn.execute(
        """
        INSERT INTO community_poll_votes (
            post_id, option_id, voter_id, department_id, created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (post_id, int(option_id), user["id"], user["department_id"], now_iso()),
    )
    conn.commit()
    conn.close()
    flash("Poll vote recorded.", "success")
    return redirect(url_for("community"))


@app.route("/community/review/<content_type>/<int:content_id>", methods=["POST"])
@roles_required("super_admin", "department_admin")
def review_content(content_type: str, content_id: int):
    user = current_user()
    decision = normalize_text(request.form.get("decision"))
    if decision not in {"approved", "rejected"}:
        flash("Invalid moderation decision.", "error")
        return redirect(url_for("community"))

    conn = get_conn()
    if content_type == "post":
        item = conn.execute("SELECT id, department_id FROM community_posts WHERE id = ?", (content_id,)).fetchone()
        if not item:
            conn.close()
            abort(404)
        require_department_access(user, item["department_id"])
        conn.execute(
            "UPDATE community_posts SET moderation_status = ?, moderation_reason = ? WHERE id = ?",
            (decision, f"Reviewed by admin on {today_string()}", content_id),
        )
    elif content_type == "comment":
        item = conn.execute("SELECT id, department_id FROM community_comments WHERE id = ?", (content_id,)).fetchone()
        if not item:
            conn.close()
            abort(404)
        require_department_access(user, item["department_id"])
        conn.execute(
            "UPDATE community_comments SET moderation_status = ?, moderation_reason = ? WHERE id = ?",
            (decision, f"Reviewed by admin on {today_string()}", content_id),
        )
    else:
        conn.close()
        abort(404)

    conn.commit()
    conn.close()
    flash("Content review saved.", "success")
    return redirect(url_for("community"))


@app.route("/api/student_performance/<path:matric>")
@login_required
def api_student_performance(matric: str):
    student = get_account_by_identifier(matric)
    if not student or student["role"] != "student":
        return jsonify({"error": "Student not found."}), 404
    user = current_user()
    if user["role"] == "student" and user["id"] != student["id"]:
        return jsonify({"error": "Forbidden."}), 403
    if user["role"] == "department_admin" and user["department_id"] != student["department_id"]:
        return jsonify({"error": "Forbidden."}), 403
    performance = get_student_performance(student["id"])
    return jsonify(
        {
            "matric": student["matric_number"],
            "name": student["full_name"],
            "overall_percent": performance["overall_percent"],
            "eligible": performance["overall_percent"] >= 70,
            "courses": performance["courses"],
        }
    )


@app.route("/api/performance")
@roles_required("super_admin", "department_admin")
def api_performance():
    user = current_user()
    conn = get_conn()
    if user["role"] == "super_admin":
        students = conn.execute(
            "SELECT id, full_name, matric_number FROM accounts WHERE role = 'student' AND is_active = 1"
        ).fetchall()
    else:
        students = conn.execute(
            """
            SELECT id, full_name, matric_number
            FROM accounts
            WHERE role = 'student' AND department_id = ? AND is_active = 1
            """,
            (user["department_id"],),
        ).fetchall()
    conn.close()

    rows = []
    for student in students:
        performance = get_student_performance(student["id"])
        for course in performance["courses"]:
            rows.append(
                {
                    "student_id": student["matric_number"],
                    "name": student["full_name"],
                    "course": course["code"],
                    "attended": course["attended"],
                    "total_classes": course["total_sessions"],
                    "attendance_percent": course["attendance_percent"],
                    "eligible": course["eligible"],
                }
            )
    return jsonify(rows)


@app.route("/api/course_average")
@roles_required("super_admin", "department_admin")
def api_course_average():
    user = current_user()
    analytics = []
    for department in get_accessible_departments_for_user(user):
        if department:
            analytics.extend(get_department_course_analytics(department["id"]))
    return jsonify(
        [
            {
                "course": item["code"],
                "total_classes": item["total_sessions"],
                "avg_attendance": item["average_attendance"],
            }
            for item in analytics
        ]
    )


@app.errorhandler(403)
def forbidden(_error):
    return render_template("forbidden.html"), 403


initialize_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
