# Smart Campus Management System

Flask-based campus platform with QR attendance, department-scoped RBAC, digital student ID cards, and a lightweight student community feed.

## Features

- Super admin, department admin, and student roles
- Department-isolated students, courses, attendance, and approvals
- QR attendance sessions with duplicate prevention
- Attendance analytics and exam eligibility tracking
- Printable digital ID cards with QR reuse on reprint approval
- Community posts, comments, likes, polls, and basic moderation

## Local Run

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python server.py
```

Open `http://127.0.0.1:5000`.

Default super admin login:

- Username: `superadmin`
- Password: `admin123`

## Railway Deploy

This repo is already prepared for Railway:

- `railway.json` sets the start command to Gunicorn
- `/health` is available for Railway health checks
- the app automatically uses `RAILWAY_VOLUME_MOUNT_PATH` when a volume is attached

### 1. Create the Railway project

1. Create a new Railway project.
2. Choose `Deploy from GitHub repo`.
3. Select this repository.

### 2. Attach a persistent volume

1. Add one volume to the web service.
2. Mount it to `/data`.

The application will automatically store:

- SQLite database in `/data/attendance.db`
- uploaded profile photos in `/data/media/uploads/profiles`
- receipt uploads in `/data/media/uploads/receipts`
- generated QR images in `/data/media/qr`

### 3. Add environment variables

Set this in Railway Variables:

```text
SMART_CAMPUS_SECRET=replace-with-a-long-random-secret
```

Optional overrides:

```text
SMART_CAMPUS_DATA_ROOT=/data
SMART_CAMPUS_SKIP_BOOTSTRAP=1
```

Notes:

- `SMART_CAMPUS_DATA_ROOT` is optional when you mount the volume at `/data`, because Railway already exposes `RAILWAY_VOLUME_MOUNT_PATH`.
- `SMART_CAMPUS_SKIP_BOOTSTRAP=1` gives you a clean empty deployment.

### 4. First deploy behavior

On the first boot with a new Railway volume, the app will:

- create the database if it does not exist
- copy the current bundled `attendance.db` into the volume if present
- copy any existing legacy `static/qr` and `static/uploads` files into the volume if they are missing there

That makes moving this project to Railway smoother without breaking existing records.

### 5. Expose the app

1. Open the Railway service.
2. Go to `Settings` -> `Networking`.
3. Click `Generate Domain`.

### 6. Health check

Set the health check path to:

```text
/health
```

## Environment Variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `SMART_CAMPUS_SECRET` | Flask session secret | `smart-campus-change-me` |
| `SMART_CAMPUS_DATA_ROOT` | Root folder for database and media | `RAILWAY_VOLUME_MOUNT_PATH` or project root |
| `SMART_CAMPUS_DB` | Database path, absolute or relative to data root | `attendance.db` |
| `SMART_CAMPUS_MEDIA_ROOT` | Media folder path, absolute or relative to data root | `media` |
| `SMART_CAMPUS_SKIP_BOOTSTRAP` | Skip seeding volume from bundled local files | unset |
| `PORT` | Runtime port for local/hosted execution | `5000` locally, Railway injects its own value |

## Deployment Notes

- This deployment path is optimized for Railway volumes and SQLite.
- If you later move to PostgreSQL, we can refactor the storage and database layer without changing the UI.
