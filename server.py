from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_from_directory, send_file
from flask_socketio import SocketIO
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import pandas as pd
from datetime import datetime
import pytz
import qrcode, os, tempfile, traceback, logging, uuid

# ---------------------- App setup ----------------------
app = Flask(__name__)
app.secret_key = "super_secret_key_change_me"  # CHANGE this before production

# ---------------------- SocketIO setup ----------------------
# Force Eventlet for async mode
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')


# ---------------------- Files & folders ----------------------
DB_FILE = "attendance.db"
QR_FOLDER_LEGACY = "static/qrcodes"
QR_FOLDER = "static/qr"
os.makedirs(QR_FOLDER_LEGACY, exist_ok=True)
os.makedirs(QR_FOLDER, exist_ok=True)

# ---------------------- SQLite helpers ----------------------
def get_conn():
    conn = sqlite3.connect(DB_FILE, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    conn.row_factory = sqlite3.Row
    return conn

def initialize_db():
    """Create tables and default users if missing."""
    conn = get_conn()
    cur = conn.cursor()

    # users table: store username, password (hashed), role
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','student'))
    )
    """)

    # attendance table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id TEXT NOT NULL,
        name TEXT NOT NULL,
        date TEXT NOT NULL,    -- 'YYYY-MM-DD'
        time TEXT NOT NULL,    -- 'HH:MM:SS'
        course TEXT
    )
    """)

    # qr_codes table for generated QR records (public generator stores here)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS qr_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        matric_number TEXT NOT NULL,
        qr_path TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL
    )
    """)

    # courses table (unique course names)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS courses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        course_name TEXT UNIQUE NOT NULL
    )
    """)

    # active_course: which course(s) are active on a given date
    cur.execute("""
    CREATE TABLE IF NOT EXISTS active_course (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        course_id INTEGER NOT NULL,
        active_date TEXT NOT NULL,
        FOREIGN KEY (course_id) REFERENCES courses(id)
    )
    """)

    # Ensure default users exist (admin / student)
    cur.execute("SELECT username FROM users WHERE username = ?", ("admin",))
    if cur.fetchone() is None:
        cur.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                    ("admin", generate_password_hash("admin123"), "admin"))
        logging.info("Created default user: admin")

    cur.execute("SELECT username FROM users WHERE username = ?", ("student",))
    if cur.fetchone() is None:
        cur.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                    ("student", generate_password_hash("student123"), "student"))
        logging.info("Created default user: student")

    conn.commit()
    conn.close()

# Initialize DB on startup
initialize_db()

# ---------------------- Helper functions ----------------------
def norm_id(s): return str(s).strip().lower() if s is not None else ''

def id_matches(stored_id, scanned_id): return norm_id(stored_id) == norm_id(scanned_id)

def safe_filename(s: str) -> str:
    return s.replace("/", "_").replace("\\", "_").replace(" ", "_")

# ---------------------- User management (DB-backed) ----------------------
def get_user(username):
    if not username:
        return None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT username, password, role FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"username": row["username"], "password": row["password"], "role": row["role"]}

def update_password(username, new_hashed_pw):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET password = ? WHERE username = ?", (new_hashed_pw, username))
    conn.commit()
    conn.close()

# ---------------------- Middleware ----------------------
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
import io

# ==========================================================
# ✅ Export Professional Attendance PDF (Logo + Header + Page #)
# ==========================================================
@app.route('/export_pdf', methods=['POST'])
def export_pdf():
    try:
        data = request.get_json() or {}
        date_filter = data.get("date", "").strip()
        course_filter = data.get("course", "").strip()

        conn = sqlite3.connect("attendance.db")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # ✅ Query with Course + Date filters
        query = "SELECT student_id, name, course, date, time FROM attendance WHERE 1=1"
        params = []

        if date_filter:
            query += " AND date = ?"
            params.append(date_filter)

        if course_filter:
            query += " AND course = ?"
            params.append(course_filter)

        query += " ORDER BY time ASC"
        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()

        # ✅ PDF in-memory
        buffer = io.BytesIO()
        pdf = SimpleDocTemplate(
            buffer, pagesize=A4, rightMargin=25, leftMargin=25, topMargin=110, bottomMargin=40
        )

        elements = []
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            'HeaderTitle',
            fontSize=14,
            alignment=1,
            spaceAfter=4,
            leading=18,
            textColor=colors.HexColor("#000000")
        )

        info_style = ParagraphStyle(
            'Info',
            fontSize=11,
            alignment=1,
            spaceAfter=2,
            leading=14
        )

        # ✅ Logo at the top center
        logo = "logo.jpg"  # Make sure correct file exists
        elements.append(Image(logo, width=80, height=80))
        elements.append(Spacer(1, 4))

        # ✅ School & Dept
        elements.append(Paragraph("Kaduna State University", title_style))
        elements.append(Spacer(1, 6))

        # ✅ Title: Attendance Report
        report_title = f"QR Attendance Report — {course_filter} ({date_filter})"
        elements.append(Paragraph(report_title, title_style))
        elements.append(Spacer(1, 10))

        # ✅ Table headings
        table_data = [["S/N", "Matric", "Name", "Course", "Date", "Time"]]

        # ✅ Table Rows
        for idx, row in enumerate(rows, start=1):
            table_data.append([
                idx,
                row["student_id"],
                row["name"],
                row["course"],
                row["date"],
                row["time"]
            ])

        table = Table(table_data, repeatRows=1)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#003366")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('GRID', (0, 0), (-1, -1), 0.8, colors.grey),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4ff")])
        ]))

        elements.append(table)

        # ✅ Page Number on each page
        def add_page_number(canvas, doc):
            page_num = canvas.getPageNumber()
            canvas.setFont("Helvetica", 9)
            canvas.drawRightString(A4[0] - 30, 20, f"Page {page_num}")

        pdf.build(elements, onFirstPage=add_page_number, onLaterPages=add_page_number)

        buffer.seek(0)
        return send_file(
            buffer,
            mimetype="application/pdf",
            as_attachment=True,
            download_name="Attendance_Report.pdf"
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.before_request
def skip_ngrok_warning():
    host = request.host or ""
    if any(x in host for x in ("ngrok.io", "ngrok-free.app", "ngrok-free.dev")):
        request.headers = {**request.headers, "ngrok-skip-browser-warning": "true"}

# ---------------------- Authentication (login page) ----------------------
@app.route('/', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = (request.form.get('password') or '').strip()
        user = get_user(username)
        if user and check_password_hash(user['password'], password):
            session['user'] = username
            session['role'] = user['role']
            logging.info(f"User '{username}' logged in as {user['role']}")
            return redirect(url_for('home'))
        error = "Invalid username or password"
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    user = session.get('user', 'Unknown')
    session.clear()
    logging.info(f"User '{user}' logged out")
    return redirect(url_for('login'))

@app.route('/change_password', methods=['GET', 'POST'])
def change_password():
    if 'user' not in session:
        return redirect(url_for('login'))

    message = ""
    if request.method == 'POST':
        current_pw = request.form.get('current_password', '').strip()
        new_pw = request.form.get('new_password', '').strip()
        confirm_pw = request.form.get('confirm_password', '').strip()
        username = session.get('user')
        user = get_user(username)

        if not user or not check_password_hash(user['password'], current_pw):
            message = "❌ Current password is incorrect."
        elif not new_pw:
            message = "⚠️ New password cannot be empty."
        elif new_pw != confirm_pw:
            message = "⚠️ Passwords do not match."
        else:
            update_password(username, generate_password_hash(new_pw))
            message = "✅ Password changed successfully."

    return render_template('change_password.html', message=message)

# ---------------------- Main pages ----------------------
@app.route('/home')
def home():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('index.html')

@app.route('/scan')
def scan():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('scan.html')

@app.route('/generate')
def generate():
    return render_template('generate.html')

@app.route('/history')
def history():
    if 'user' not in session or session.get('role') != 'admin':
        return redirect(url_for('login'))
    return render_template('history.html', data=[])

# ---------------------- Course endpoints ----------------------
@app.route('/set_course', methods=['POST'])
def set_course():
    """
    Admin posts JSON: { "course": "...", "mode": "general" | "course" }.
    This will:
      - ensure the course exists in courses table
      - insert a row into active_course for today's date (idempotent)
    """
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({"message": "Unauthorized"}), 403

    info = request.get_json() or {}
    course = (info.get('course') or '').strip()
    mode = (info.get('mode') or 'general').strip()

    conn = get_conn()
    cur = conn.cursor()
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        if mode == 'course':
            if not course:
                return jsonify({"message": "Course required for course mode"}), 400
            # ensure course exists
            cur.execute("SELECT id FROM courses WHERE LOWER(TRIM(course_name)) = ?", (course.lower().strip(),))
            r = cur.fetchone()
            if r:
                course_id = r['id']
            else:
                cur.execute("INSERT INTO courses (course_name) VALUES (?)", (course,))
                course_id = cur.lastrowid

            # ensure active_course row for today + course exists (idempotent)
            cur.execute("SELECT 1 FROM active_course WHERE course_id = ? AND active_date = ? LIMIT 1", (course_id, today))
            if not cur.fetchone():
                cur.execute("INSERT INTO active_course (course_id, active_date) VALUES (?, ?)", (course_id, today))
        else:
            # mode == 'general' -> remove today's active_course rows
            cur.execute("DELETE FROM active_course WHERE active_date = ?", (today,))

        conn.commit()
        logging.info(f"Course set: '{course}' (mode: {mode})")
        # notify connected dashboards
        try:
            socketio.emit('course_changed', {"course": course if mode == 'course' else "", "mode": mode, "timestamp": datetime.utcnow().isoformat()})
        except Exception:
            pass
        return jsonify({"message": "Course and mode updated", "course": course, "mode": mode})
    except Exception as e:
        conn.rollback()
        logging.error(f"set_course error: {e}")
        traceback.print_exc()
        return jsonify({"message": "Failed", "error": str(e)}), 500
    finally:
        conn.close()

@app.route('/get_course', methods=['GET'])
def get_course():
    """
    Returns the active course for today, if any:
      { "active": True/False, "course": "...", "mode": "course"|"general", "course_id": N }
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        cur.execute("""
            SELECT a.course_id, c.course_name
            FROM active_course a
            JOIN courses c ON c.id = a.course_id
            WHERE a.active_date = ?
            ORDER BY a.id DESC LIMIT 1
        """, (today,))
        r = cur.fetchone()
        if not r:
            return jsonify({"active": False, "course": "", "mode": "general", "course_id": None})
        return jsonify({"active": True, "course": r["course_name"], "mode": "course", "course_id": r["course_id"]})
    except Exception as e:
        logging.error(f"get_course error: {e}")
        traceback.print_exc()
        return jsonify({"active": False, "course": "", "mode": "general", "course_id": None})
    finally:
        conn.close()

@app.route('/clear_course', methods=['POST'])
def clear_course():
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({"message": "Unauthorized"}), 403
    conn = get_conn()
    cur = conn.cursor()
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        cur.execute("DELETE FROM active_course WHERE active_date = ?", (today,))
        conn.commit()
        try:
            socketio.emit('course_changed', {"course": "", "mode": "general", "timestamp": datetime.utcnow().isoformat()})
        except Exception:
            pass
        return jsonify({"message": "Session cleared (no active course)"}), 200
    except Exception as e:
        logging.error(f"clear_course error: {e}")
        traceback.print_exc()
        return jsonify({"message": "Failed", "error": str(e)}), 500
    finally:
        conn.close()

# ---------------------- QR generator ----------------------
@app.route('/generate_qr', methods=['POST'])
def generate_qr():
    try:
        info = request.get_json() or {}
        student_id = (info.get('id') or '').strip()
        name = (info.get('name') or '').strip()
        if not student_id or not name:
            return jsonify({"message": "Name and Matric number are required"}), 400

        qr_data = f"{student_id}|{name}"
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        safe_id = safe_filename(student_id)
        unique = uuid.uuid4().hex[:8]
        filename = f"{safe_id}_{timestamp}_{unique}.png"
        filepath = os.path.join(QR_FOLDER, filename)

        qr_img = qrcode.make(qr_data)
        qr_img.save(filepath)

        conn = get_conn()
        cur = conn.cursor()
        created_at = datetime.utcnow().isoformat()
        qr_path = f"/static/qr/{filename}"
        cur.execute("""
            INSERT INTO qr_codes (name, matric_number, qr_path, status, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (name, student_id, qr_path, 'pending', created_at))
        conn.commit()
        conn.close()

        logging.info(f"Generated public QR for {name} ({student_id}) -> {filename}")
        return jsonify({"message": "QR Code generated!", "qr_path": qr_path})
    except Exception as e:
        logging.error(f"Failed to generate QR: {e}")
        traceback.print_exc()
        return jsonify({"message": "Failed to generate QR", "error": str(e)}), 500
    socketio.emit("attendance_update", {"matric": student_id})
    
def now_lagos():
    return datetime.now(pytz.timezone("Africa/Lagos"))
# ---------------------- Attendance marking ----------------------
@app.route('/mark_attendance', methods=['POST'])
def mark_attendance():
    try:
        data = request.get_json() or {}
        payload = (data.get('data') or '').strip()
        if '|' not in payload:
            socketio.emit('attendance_update', {'status': 'new record added'}, broadcast=True)
            return jsonify({"success": False, "message": "Invalid QR content (expected 'id|name')"}), 400
        student_id, name = map(str.strip, payload.split('|', 1))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "message": "Invalid QR content", "error": str(e)}), 400

    now = now_lagos()
    date_today = now.strftime('%Y-%m-%d')
    time_now = now.strftime('%H:%M:%S')
    norm_scanned = norm_id(student_id)

    conn = get_conn()
    cur = conn.cursor()
    try:
        # Get today's active course (if any)
        cur.execute("""
            SELECT a.course_id, c.course_name
            FROM active_course a
            JOIN courses c ON c.id = a.course_id
            WHERE a.active_date = ?
            ORDER BY a.id DESC LIMIT 1
        """, (date_today,))
        active = cur.fetchone()
        active_mode = "course" if active else "general"
        course_name = active['course_name'] if active else "General"

        messages = []
        inserted_records = []

        # ------------------ General attendance check ------------------
        cur.execute("""
            SELECT 1 FROM attendance
            WHERE date = ? AND LOWER(TRIM(student_id)) = ? AND LOWER(TRIM(course)) = 'general'
            LIMIT 1
        """, (date_today, norm_scanned))
        general_exists = cur.fetchone() is not None

        if not general_exists:
            cur.execute("""
                INSERT INTO attendance (student_id, name, date, time, course)
                VALUES (?, ?, ?, ?, ?)
            """, (student_id, name, date_today, time_now, 'General'))
            conn.commit()
            messages.append("✅ General attendance recorded.")
            inserted_records.append({"ID": student_id, "Name": name, "Date": date_today, "Time": time_now, "Course": "General"})
        else:
            messages.append("⚠️ General attendance already recorded today.")

        # ------------------ Course attendance check ------------------
        if active_mode == "course":
            cur.execute("""
                SELECT 1 FROM attendance
                WHERE date = ? AND LOWER(TRIM(student_id)) = ? AND LOWER(TRIM(course)) = ?
                LIMIT 1
            """, (date_today, norm_scanned, course_name.lower()))
            course_exists = cur.fetchone() is not None

            if not course_exists:
                cur.execute("""
                    INSERT INTO attendance (student_id, name, date, time, course)
                    VALUES (?, ?, ?, ?, ?)
                """, (student_id, name, date_today, time_now, course_name))
                conn.commit()
                messages.append(f"✅ Course attendance recorded for '{course_name}'.")
                inserted_records.append({"ID": student_id, "Name": name, "Date": date_today, "Time": time_now, "Course": course_name})
            else:
                messages.append(f"⚠️ Course attendance already recorded for '{course_name}'.")

        # ------------------ SocketIO notifications ------------------
        try:
            for rec in inserted_records:
                socketio.emit('new_attendance', rec)

            if active_mode == "course":
                cur.execute("""
                    SELECT COUNT(DISTINCT student_id) AS cnt FROM attendance
                    WHERE date = ? AND LOWER(TRIM(course)) = ?
                """, (date_today, course_name.lower()))
                cnt_row = cur.fetchone()
                count = int(cnt_row['cnt'] or 0) if cnt_row else 0
                socketio.emit('attendance_count', {"course": course_name, "count": count, "date": date_today})
        except Exception as e:
            logging.warning(f"SocketIO emit failed: {e}")

        return jsonify({
            "success": True,
            "messages": messages,
            "inserted": inserted_records
        })

    except Exception as e:
        logging.error(f"Error marking attendance: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": "Failed to mark attendance", "error": str(e)}), 500
    finally:
        conn.close()

# ---------------------- History endpoints ----------------------
@app.route('/history_by_date')
def history_by_date():
    if 'user' not in session:
        return jsonify([]), 401

    qdate = (request.args.get('date') or '').strip()
    qcourse = (request.args.get('course') or '').strip().lower()

    conn = get_conn()
    cur = conn.cursor()
    try:
        query = "SELECT student_id AS ID, name AS Name, date AS Date, time AS Time, course AS Course FROM attendance WHERE 1=1"
        params = []
        if qdate:
            query += " AND date = ?"
            params.append(qdate)
        if qcourse:
            query += " AND LOWER(TRIM(course)) = ?"
            params.append(qcourse)
        query += " ORDER BY date DESC, time DESC"
        cur.execute(query, params)
        rows = cur.fetchall()
        records = [dict(r) for r in rows]
        return jsonify(records)
    except Exception as e:
        logging.error(f"Failed to read attendance for history: {e}")
        traceback.print_exc()
        return jsonify([])
    finally:
        conn.close()

@app.route('/download_by_date')
def download_by_date():
    if 'user' not in session:
        return "Unauthorized", 401

    qdate = (request.args.get('date') or '').strip()
    qcourse = (request.args.get('course') or '').strip().lower()

    conn = get_conn()
    cur = conn.cursor()
    tmp_name = None
    try:
        query = "SELECT student_id AS ID, name AS Name, date AS Date, time AS Time, course AS Course FROM attendance WHERE 1=1"
        params = []
        if qdate:
            query += " AND date = ?"
            params.append(qdate)
        if qcourse:
            query += " AND LOWER(TRIM(course)) = ?"
            params.append(qcourse)
        query += " ORDER BY date ASC, time ASC"
        cur.execute(query, params)
        rows = cur.fetchall()
        if not rows:
            return "No data for selection", 404

        df = pd.DataFrame([dict(r) for r in rows])
        out_name = f"attendance_{qdate or 'all'}_{qcourse or 'general'}.xlsx"

        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
            tmp_name = tmp.name
        df.to_excel(tmp_name, index=False)

        return send_file(tmp_name, as_attachment=True, download_name=out_name)
    except Exception as e:
        logging.error(f"Failed to prepare download: {e}")
        traceback.print_exc()
        return "Server error", 500
    finally:
        conn.close()
        try:
            if tmp_name and os.path.exists(tmp_name):
                os.remove(tmp_name)
        except Exception:
            pass

@app.route('/delete_history', methods=['POST'])
def delete_history():
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({"message": "Unauthorized"}), 403
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM attendance")
        conn.commit()
        logging.info("Attendance history deleted")
        return jsonify({"message": "All attendance history deleted."})
    except Exception as e:
        logging.error(f"Failed to delete history: {e}")
        traceback.print_exc()
        return jsonify({"message": "Failed to delete history", "error": str(e)}), 500
    finally:
        conn.close()

@app.route('/delete_record', methods=['POST'])
def delete_record():
    data = request.get_json()
    student_id = data.get('ID')
    date = data.get('Date')
    time = data.get('Time')

    try:
        conn = sqlite3.connect('attendance.db')
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM attendance WHERE student_id=? AND date=? AND time=?",
            (student_id, date, time)
        )
        conn.commit()
        conn.close()

        return jsonify({"success": True})
    except Exception as e:
        print("Delete error:", e)
        return jsonify({"success": False})


# ---------------------- My attendance ----------------------
@app.route('/my_attendance', methods=['GET', 'POST'])
def my_attendance():
    query_id = (request.form.get('id') or '').strip() if request.method == 'POST' else ''
    records = []
    if query_id:
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT student_id AS ID, name AS Name, date AS Date, time AS Time, course AS Course
                FROM attendance
                ORDER BY date DESC, time DESC
            """)
            rows = cur.fetchall()
            for r in rows:
                if id_matches(r["ID"], query_id):
                    records.append(dict(r))
        except Exception as e:
            logging.error(f"Failed to read attendance in my_attendance: {e}")
            traceback.print_exc()
        finally:
            conn.close()
    return render_template('my_attendance.html', records=records, query_id=query_id)

# ---------------------- QR serving ----------------------
@app.route('/static/qr/<path:filename>')
def qr_file(filename):
    return send_from_directory(QR_FOLDER, filename)

# ---------------------- Attendance performance & chart API ----------------------
@app.route('/api/performance')
def api_performance():
    try:
        conn = get_conn()
        df = pd.read_sql_query("SELECT * FROM attendance", conn)
        conn.close()

        if df.empty:
            return jsonify([])

        # ✅ Get total unique class sessions (global for each course)
        total_classes = df.groupby('course')['date'].nunique().to_dict()

        performance = []
        for (student_id, course), group in df.groupby(['student_id', 'course']):
            attended = group['date'].nunique()
            total = total_classes.get(course, 1)
            percent = (attended / total) * 100
            performance.append({
                "student_id": student_id,
                "name": group['name'].iloc[0],
                "course": course,
                "attended": attended,
                "total_classes": total,
                "attendance_percent": round(percent, 2),
                "eligible": percent >= 70
            })

        return jsonify(performance)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/course_average')
def api_course_average():
    try:
        conn = get_conn()
        df = pd.read_sql_query("SELECT * FROM attendance", conn)
        conn.close()

        if df.empty:
            return jsonify([])

        # ✅ Compute total unique sessions per course
        total_classes = df.groupby('course')['date'].nunique().to_dict()
        results = []

        for course, group in df.groupby('course'):
            total = total_classes.get(course, 1)

            # Compute each student's attendance percentage for this course
            student_percents = []
            for sid, g in group.groupby('student_id'):
                attended = g['date'].nunique()
                percent = (attended / total) * 100
                student_percents.append(percent)

            # Average attendance across all students
            avg_percent = sum(student_percents) / len(student_percents)
            results.append({
                "course": course,
                "total_classes": total,
                "avg_attendance": round(avg_percent, 2)
            })

        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/student_performance/<path:matric>')
def api_student_performance(matric):
    try:
        conn = sqlite3.connect('attendance.db')

        # ✅ Get full attendance to know total class sessions globally
        all_data = pd.read_sql_query("SELECT * FROM attendance", conn)

        # Get the specific student's attendance
        df = pd.read_sql_query(
            "SELECT * FROM attendance WHERE LOWER(student_id)=LOWER(?)",
            conn, params=(matric,)
        )
        conn.close()

        if df.empty:
            return jsonify({"error": "No attendance record found for this matric number."}), 404

        results = []
        total_percent = 0
        total_courses = 0

        for course in sorted(all_data['course'].unique()):
            total_classes = all_data[all_data['course'] == course]['date'].nunique()
            attended = df[df['course'] == course]['date'].nunique()
            percent = (attended / total_classes) * 100 if total_classes > 0 else 0

            results.append({
                "course": course,
                "total_classes": total_classes,
                "attended": attended,
                "attendance_percent": round(percent, 2),
                "eligible": percent >= 70
            })

            total_percent += percent
            total_courses += 1

        overall = total_percent / total_courses if total_courses > 0 else 0
        eligible = overall >= 70

        return jsonify({
            "matric": matric.upper(),
            "name": df['name'].iloc[0],
            "overall_percent": round(overall, 2),
            "eligible": eligible,
            "courses": results
        })

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


# ---------------------- Run ----------------------
if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
