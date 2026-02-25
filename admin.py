"""
Admin Panel - Role-based permissions with comment restrictions
"""
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, current_app
from functools import wraps
from datetime import datetime, timedelta
import os
import sqlite3
import re
import json
from collections import defaultdict

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

ROLE_HIERARCHY = {'user': 0, 'host': 1, 'mod': 2, 'co_owner': 3, 'owner': 4}

ROLE_CODES = {
    'host': os.environ.get('HOST_CODE', 'HOST123'),
    'mod': os.environ.get('MOD_CODE', 'MOD456'),
    'co_owner': os.environ.get('CO_OWNER_CODE', 'COOWNER789'),
    'owner': os.environ.get('OWNER_CODE', 'OWNER999')
}

# Permissions by role
ROLE_PERMISSIONS = {
    'host': ['ban', 'timeout', 'restrict_comments', 'view_users', 'view_bans', 'view_audit'],
    'mod': ['ban', 'timeout', 'restrict_comments', 'view_users', 'view_bans', 'view_audit', 'delete_comments'],
    'co_owner': ['ban', 'timeout', 'restrict_comments', 'view_users', 'view_bans', 'view_audit', 'delete_comments', 
                 'change_roles', 'view_settings', 'manage_xp'],
    'owner': ['ban', 'timeout', 'restrict_comments', 'view_users', 'view_bans', 'view_audit', 'delete_comments',
              'change_roles', 'view_settings', 'edit_settings', 'manage_xp', 'full_access']
}

def get_db():
    from app import get_db as app_get_db
    return app_get_db()

def get_admin_session():
    if 'admin_role' not in session:
        return None
    return {'role': session.get('admin_role'), 'level': ROLE_HIERARCHY.get(session.get('admin_role'), 0)}

def has_permission(permission):
    """Check if current admin has specific permission"""
    admin = get_admin_session()
    if not admin:
        return False
    return permission in ROLE_PERMISSIONS.get(admin['role'], [])

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        admin = get_admin_session()
        if not admin:
            if request.is_json or request.path.startswith('/admin/api/'):
                return jsonify({"error": "Admin login required"}), 401
            return redirect(url_for('admin.admin_login'))
        return f(*args, **kwargs)
    return decorated_function

def require_permission(permission):
    """Decorator to require specific permission"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not has_permission(permission):
                return jsonify({"error": "Permission denied"}), 403
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def can_modify_role(admin_role, target_role):
    return ROLE_HIERARCHY.get(admin_role, 0) > ROLE_HIERARCHY.get(target_role, 0)

def _iso_now():
    return datetime.now().isoformat()

def _parse_dt(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Normalize common SQLite/Postgres timestamp forms.
    text = text.replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T")):
        try:
            return datetime.fromisoformat(candidate)
        except Exception:
            continue
    # Last-resort trim fractional offset artifacts.
    try:
        return datetime.fromisoformat(text[:19])
    except Exception:
        return None

def _ensure_admin_feature_tables(conn, c, db_type):
    if db_type == 'postgres':
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_presence (
                user_id INTEGER PRIMARY KEY,
                last_seen TIMESTAMP,
                last_path TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_notifications (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                title TEXT,
                message TEXT,
                notif_type TEXT DEFAULT 'announcement',
                source TEXT DEFAULT 'admin',
                is_read INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS admin_announcements (
                id SERIAL PRIMARY KEY,
                title TEXT,
                message TEXT,
                is_global INTEGER DEFAULT 1,
                target_user_id INTEGER,
                scheduled_for TIMESTAMP,
                status TEXT DEFAULT 'scheduled',
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS admin_chat_messages (
                id SERIAL PRIMARY KEY,
                admin_role TEXT,
                message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS donation_events (
                id SERIAL PRIMARY KEY,
                amount_cents INTEGER,
                currency TEXT DEFAULT 'usd',
                status TEXT DEFAULT 'paid',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_presence (
                user_id INTEGER PRIMARY KEY,
                last_seen TEXT,
                last_path TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                title TEXT,
                message TEXT,
                notif_type TEXT DEFAULT 'announcement',
                source TEXT DEFAULT 'admin',
                is_read INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                sent_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS admin_announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                message TEXT,
                is_global INTEGER DEFAULT 1,
                target_user_id INTEGER,
                scheduled_for TEXT,
                status TEXT DEFAULT 'scheduled',
                created_by TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                sent_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS admin_chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_role TEXT,
                message TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS donation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount_cents INTEGER,
                currency TEXT DEFAULT 'usd',
                status TEXT DEFAULT 'paid',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
    conn.commit()

def _ensure_daily_actions_schema(c, db_type):
    cols = _get_table_columns(c, db_type, 'daily_actions')
    if cols:
        if db_type == 'postgres':
            if 'user_id' not in cols:
                c.execute("ALTER TABLE daily_actions ADD COLUMN IF NOT EXISTS user_id INTEGER")
            if 'action' not in cols:
                c.execute("ALTER TABLE daily_actions ADD COLUMN IF NOT EXISTS action TEXT")
            if 'verse_id' not in cols:
                c.execute("ALTER TABLE daily_actions ADD COLUMN IF NOT EXISTS verse_id INTEGER")
            if 'event_date' not in cols:
                c.execute("ALTER TABLE daily_actions ADD COLUMN IF NOT EXISTS event_date TEXT")
            if 'timestamp' not in cols:
                c.execute("ALTER TABLE daily_actions ADD COLUMN IF NOT EXISTS timestamp TEXT")
        else:
            if 'user_id' not in cols:
                c.execute("ALTER TABLE daily_actions ADD COLUMN user_id INTEGER")
            if 'action' not in cols:
                c.execute("ALTER TABLE daily_actions ADD COLUMN action TEXT")
            if 'verse_id' not in cols:
                c.execute("ALTER TABLE daily_actions ADD COLUMN verse_id INTEGER")
            if 'event_date' not in cols:
                c.execute("ALTER TABLE daily_actions ADD COLUMN event_date TEXT")
            if 'timestamp' not in cols:
                c.execute("ALTER TABLE daily_actions ADD COLUMN timestamp TEXT")
        return

    if db_type == 'postgres':
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_actions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                action TEXT,
                verse_id INTEGER,
                event_date TEXT,
                timestamp TEXT,
                UNIQUE(user_id, action, verse_id, event_date)
            )
        """)
    else:
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT,
                verse_id INTEGER,
                event_date TEXT,
                timestamp TEXT,
                UNIQUE(user_id, action, verse_id, event_date)
            )
        """)

def _get_table_columns(c, db_type, table_name):
    """Return lowercase column names for a table."""
    cols = set()
    try:
        if db_type == 'postgres':
            c.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
            """, (table_name,))
            rows = c.fetchall()
            for row in rows:
                if hasattr(row, 'keys'):
                    try:
                        cols.add(str(row['column_name']).lower())
                    except Exception:
                        try:
                            row = _row_to_dict(row)
                            cols.add(str(row.get('column_name', '')).lower())
                        except Exception:
                            pass
                else:
                    cols.add(str(row[0]).lower())
        else:
            c.execute(f"PRAGMA table_info({table_name})")
            rows = c.fetchall()
            for row in rows:
                cols.add(str(row[1]).lower())
    except Exception as e:
        print(f"[WARN] Could not read columns for {table_name}: {e}")
    return cols

def _row_to_dict(row):
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    if hasattr(row, 'keys'):
        try:
            return {k: row[k] for k in row.keys()}
        except Exception:
            return row
    return row

def _row_first_value(row, default=0):
    if row is None:
        return default
    if isinstance(row, dict):
        return next(iter(row.values()), default)
    if hasattr(row, 'keys'):
        try:
            return row[0]
        except Exception:
            try:
                keys = row.keys()
                return row[keys[0]] if keys else default
            except Exception:
                return default
    try:
        return row[0]
    except Exception:
        return default

def _ensure_audit_logs_schema(conn, c, db_type):
    """Create/migrate audit_logs so queries work across older DB schemas."""
    if db_type == 'postgres':
        c.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id SERIAL PRIMARY KEY,
                admin_id TEXT,
                action TEXT,
                target_user_id INTEGER,
                details TEXT,
                ip_address TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        c.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id TEXT,
                action TEXT,
                target_user_id INTEGER,
                details TEXT,
                ip_address TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    conn.commit()

    cols = _get_table_columns(c, db_type, 'audit_logs')
    required = {
        'admin_id': 'TEXT',
        'action': 'TEXT',
        'target_user_id': 'INTEGER',
        'details': 'TEXT',
        'ip_address': 'TEXT',
        'timestamp': 'TIMESTAMP' if db_type == 'postgres' else 'TEXT'
    }

    for col, col_type in required.items():
        if col in cols:
            continue
        try:
            if db_type == 'postgres':
                c.execute(f"ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS {col} {col_type}")
            else:
                c.execute(f"ALTER TABLE audit_logs ADD COLUMN {col} {col_type}")
            conn.commit()
            cols.add(col)
        except Exception as e:
            if db_type == 'postgres':
                conn.rollback()
            print(f"[WARN] Could not add audit_logs.{col}: {e}")

    # Backfill timestamp from created_at for legacy tables when available.
    if 'timestamp' in cols and 'created_at' in cols:
        try:
            c.execute("UPDATE audit_logs SET timestamp = created_at WHERE timestamp IS NULL AND created_at IS NOT NULL")
            conn.commit()
        except Exception as e:
            if db_type == 'postgres':
                conn.rollback()
            print(f"[WARN] Could not backfill audit_logs.timestamp: {e}")

def _extract_target_user_id(details):
    if not details:
        return None
    text = str(details)
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                for key in ("target_user_id", "user_id", "uid", "target_id"):
                    val = obj.get(key)
                    if val is not None and str(val).strip().isdigit():
                        return int(str(val).strip())
        except Exception:
            pass
    patterns = [
        r"\((\d+)\)",               # "... (123)"
        r"\buser[_\s]*id[:=\s]+(\d+)\b",
        r"\buser\s+(\d+)\b",
        r"\bid[:=\s]+(\d+)\b"
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except Exception:
                return None
    return None

def _parse_details_fields(details):
    """Extract normalized fields from JSON or legacy text details."""
    raw = details if details is not None else ""
    text = str(raw).strip()
    parsed = {}

    if not text:
        return parsed

    # JSON details
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                # New structured format
                msg = obj.get("message")
                if msg is not None:
                    parsed["message"] = str(msg)

                status_val = obj.get("status")
                if status_val is not None:
                    parsed["status"] = str(status_val).strip().lower()

                location_val = obj.get("location")
                if isinstance(location_val, dict):
                    parsed["location"] = location_val

                extras_val = obj.get("extras")
                if isinstance(extras_val, dict):
                    parsed["extras"] = extras_val

                target_val = obj.get("target")
                if isinstance(target_val, dict):
                    parsed["target"] = target_val
                    tname = target_val.get("name")
                    if tname:
                        parsed["target_name_hint"] = str(tname)

                reason_val = (
                    obj.get("reason")
                    or obj.get("ban_reason")
                    or obj.get("restriction_reason")
                    or obj.get("reason_text")
                )
                duration_val = obj.get("duration") or obj.get("ban_duration") or obj.get("hours")
                target_name_val = (
                    obj.get("user_name")
                    or obj.get("target_name")
                    or obj.get("name")
                    or obj.get("username")
                )
                if reason_val is not None:
                    parsed["reason"] = str(reason_val)
                if duration_val is not None:
                    parsed["duration"] = str(duration_val)
                if target_name_val is not None:
                    parsed["target_name_hint"] = str(target_name_val)
                return parsed
        except Exception:
            pass

    # "Banned Name (123) for 24h: reason..."
    m = re.search(r"\bfor\s+([^:]+):\s*(.+)$", text, flags=re.IGNORECASE)
    if m:
        parsed["duration"] = m.group(1).strip()
        parsed["reason"] = m.group(2).strip()
    else:
        # "Restricted Name (123) for 6h: reason" variant
        m2 = re.search(r"\b:\s*(.+)$", text)
        if m2:
            parsed["reason"] = m2.group(1).strip()

    # Capture target name before "(id)"
    nm = re.search(r"\b(?:Banned|Unbanned|Restricted)\s+(.+?)\s+\(\d+\)", text, flags=re.IGNORECASE)
    if nm:
        parsed["target_name_hint"] = nm.group(1).strip()

    parsed["message"] = text
    return parsed

def _fetch_user_personas(c, db_type, user_ids):
    if not user_ids:
        return {}

    ordered_ids = []
    seen = set()
    for uid in user_ids:
        if uid is None:
            continue
        try:
            normalized = int(str(uid).strip())
        except Exception:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered_ids.append(normalized)
    ordered_ids.sort()
    if not ordered_ids:
        return {}

    placeholders = ','.join(['%s'] * len(ordered_ids)) if db_type == 'postgres' else ','.join(['?'] * len(ordered_ids))
    query = f"SELECT id, name, email, role FROM users WHERE id IN ({placeholders})"
    try:
        c.execute(query, tuple(ordered_ids))
        rows = c.fetchall()
    except Exception as e:
        print(f"[WARN] Could not load user personas: {e}")
        return {}

    personas = {}
    for row in rows:
        if hasattr(row, 'keys'):
            row = _row_to_dict(row)
            uid = row.get('id')
            personas[uid] = {
                "name": row.get('name') or "Unknown",
                "email": row.get('email') or "",
                "role": row.get('role') or "user"
            }
        else:
            uid = row[0]
            personas[uid] = {
                "name": row[1] or "Unknown",
                "email": row[2] or "",
                "role": row[3] or "user"
            }
    return personas

def _safe_json_dumps(value):
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return json.dumps({"message": str(value)}, ensure_ascii=False)

def _request_location_snapshot():
    # Do not call external APIs here; extract best-effort location from common proxy headers.
    forwarded_for = request.headers.get('X-Forwarded-For', '')
    ip = (forwarded_for.split(',')[0].strip() if forwarded_for else '') or request.headers.get('X-Real-IP') or request.remote_addr or 'unknown'
    return {
        "ip": ip,
        "country": request.headers.get('CF-IPCountry') or request.headers.get('X-Country-Code') or "",
        "region": request.headers.get('X-Region') or request.headers.get('CF-Region') or "",
        "city": request.headers.get('X-City') or request.headers.get('CF-IPCity') or "",
        "timezone": request.headers.get('CF-Timezone') or ""
    }

def _read_audit_logs(c, db_type, limit=100, offset=0, action=None):
    cols = _get_table_columns(c, db_type, 'audit_logs')
    if not cols:
        return [], 0

    ts_col = 'timestamp' if 'timestamp' in cols else ('created_at' if 'created_at' in cols else None)
    order_col = ts_col if ts_col else 'id'
    ts_expr = ts_col if ts_col else 'NULL'
    ip_expr = 'ip_address' if 'ip_address' in cols else 'NULL'
    target_expr = 'target_user_id' if 'target_user_id' in cols else 'NULL'

    where_sql = ""
    params = []
    if action and action.lower() != 'all':
        where_sql = "WHERE action = %s" if db_type == 'postgres' else "WHERE action = ?"
        params.append(action)

    count_query = f"SELECT COUNT(*) FROM audit_logs {where_sql}"
    c.execute(count_query, tuple(params))
    total_row = c.fetchone()
    total = int(_row_first_value(total_row, 0) or 0)

    limit_ph = "%s" if db_type == 'postgres' else "?"
    offset_ph = "%s" if db_type == 'postgres' else "?"
    query = f"""
        SELECT
            id,
            admin_id,
            action,
            details,
            {ip_expr} AS ip_address,
            {ts_expr} AS event_time,
            {target_expr} AS target_user_id
        FROM audit_logs
        {where_sql}
        ORDER BY {order_col} DESC, id DESC
        LIMIT {limit_ph} OFFSET {offset_ph}
    """
    c.execute(query, tuple(params + [limit, offset]))
    rows = c.fetchall()

    base_logs = []
    target_user_ids = set()
    admin_user_ids = set()
    for row in rows:
        row = _row_to_dict(row)
        if hasattr(row, 'keys'):
            log = {
                "id": row.get('id'),
                "admin_id": row.get('admin_id') or "system",
                "action": row.get('action') or "UNKNOWN",
                "details": row.get('details') or "",
                "ip_address": row.get('ip_address') or "",
                "timestamp": row.get('event_time'),
                "target_user_id": row.get('target_user_id')
            }
        else:
            log = {
                "id": row[0],
                "admin_id": row[1] or "system",
                "action": row[2] or "UNKNOWN",
                "details": row[3] or "",
                "ip_address": row[4] or "",
                "timestamp": row[5],
                "target_user_id": row[6]
            }

        if log["target_user_id"] is None:
            log["target_user_id"] = _extract_target_user_id(log["details"])
        if log["target_user_id"] is not None:
            target_user_ids.add(log["target_user_id"])
        try:
            admin_uid = int(str(log["admin_id"]).strip())
            admin_user_ids.add(admin_uid)
        except Exception:
            pass
        base_logs.append(log)

    personas = _fetch_user_personas(c, db_type, target_user_ids)
    admin_personas = _fetch_user_personas(c, db_type, admin_user_ids)
    logs = []
    for log in base_logs:
        persona = personas.get(log["target_user_id"])
        parsed_details = _parse_details_fields(log["details"])
        location = parsed_details.get("location") if isinstance(parsed_details.get("location"), dict) else {}
        extras = parsed_details.get("extras") if isinstance(parsed_details.get("extras"), dict) else {}
        target_obj = parsed_details.get("target") if isinstance(parsed_details.get("target"), dict) else {}
        try:
            admin_uid = int(str(log["admin_id"]).strip())
        except Exception:
            admin_uid = None
        admin_persona = admin_personas.get(admin_uid) if admin_uid is not None else None
        target_name = persona["name"] if persona else (parsed_details.get("target_name_hint") or "")
        details_message = parsed_details.get("message") or (log["details"] if log["details"] is not None else "")
        status = parsed_details.get("status") or ("success" if (log["action"] or "").upper() not in ("ERROR", "FAILED") else "failed")
        reason = parsed_details.get("reason") or (extras.get("reason") if isinstance(extras, dict) else "") or ""
        duration = parsed_details.get("duration") or (extras.get("duration") if isinstance(extras, dict) else "") or ""
        logs.append({
            "id": log["id"],
            "admin_id": log["admin_id"],
            "admin_name": (admin_persona["name"] if admin_persona else log["admin_id"]),
            "action": log["action"],
            "details": details_message,
            "ip_address": log["ip_address"],
            "timestamp": log["timestamp"],
            "created_at": log["timestamp"],  # compatibility for existing audit UI
            "status": status,
            "location": {
                "ip": location.get("ip") or log["ip_address"] or "",
                "country": location.get("country") or "",
                "region": location.get("region") or "",
                "city": location.get("city") or "",
                "timezone": location.get("timezone") or ""
            },
            "target_user_id": log["target_user_id"],
            "target_name": target_name,
            "target_email": persona["email"] if persona else (target_obj.get("email", "") if isinstance(target_obj, dict) else ""),
            "target_role": persona["role"] if persona else (target_obj.get("role", "") if isinstance(target_obj, dict) else ""),
            "target_persona": persona if persona else None,
            "target": {
                "user_id": log["target_user_id"],
                "name": target_name,
                "email": persona["email"] if persona else (target_obj.get("email", "") if isinstance(target_obj, dict) else ""),
                "role": persona["role"] if persona else (target_obj.get("role", "") if isinstance(target_obj, dict) else "")
            },
            "reason": reason,
            "duration": duration,
            "extras": extras
        })

    return logs, total

def log_action(action, details="", target_user_id=None, status="success", extras=None, target=None):
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        _ensure_audit_logs_schema(conn, c, db_type)
        admin = get_admin_session()
        admin_id = admin['role'] if admin else 'unknown'
        timestamp = datetime.now().isoformat()
        payload = {
            "message": str(details or ""),
            "status": str(status or "success").lower(),
            "location": _request_location_snapshot(),
            "extras": extras if isinstance(extras, dict) else {}
        }
        if isinstance(target, dict):
            payload["target"] = target
        elif target_user_id is not None:
            payload["target"] = {"user_id": target_user_id}
        details_json = _safe_json_dumps(payload)
        
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO audit_logs (admin_id, action, details, ip_address, timestamp, target_user_id)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (admin_id, action, details_json, payload.get("location", {}).get("ip") or request.remote_addr, timestamp, target_user_id))
        else:
            c.execute("""
                INSERT INTO audit_logs (admin_id, action, details, ip_address, timestamp, target_user_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (admin_id, action, details_json, payload.get("location", {}).get("ip") or request.remote_addr, timestamp, target_user_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[ERROR] Log action failed: {e}")

@admin_bp.route('/login')
def admin_login():
    if get_admin_session():
        return redirect(url_for('admin.admin_dashboard'))
    return render_template('admin_login_simple.html')

@admin_bp.route('/dashboard')
@admin_required
def admin_dashboard():
    return render_template('admin_dashboard.html')

@admin_bp.route('/audits')
@admin_required
@require_permission('view_audit')
def admin_audits():
    return render_template('admin_audits.html')

@admin_bp.route('/logout', methods=['POST'])
def admin_logout():
    session.pop('admin_role', None)
    session.pop('admin_code', None)
    return redirect(url_for('admin.admin_login'))

@admin_bp.route('/api/verify-code', methods=['POST'])
def verify_code():
    data = request.get_json()
    code = data.get('code', '').strip()
    
    if not code:
        return jsonify({"success": False, "error": "Code required"}), 400
    
    for role, role_code in ROLE_CODES.items():
        if code == role_code:
            session['admin_role'] = role
            session['admin_code'] = code
            # Sync role onto the logged-in user record when available
            try:
                user_id = session.get('user_id')
                if user_id:
                    conn, db_type = get_db()
                    c = conn.cursor()
                    is_admin = 1 if role in ('owner', 'co_owner', 'mod', 'host') else 0
                    if db_type == 'postgres':
                        c.execute("UPDATE users SET role = %s, is_admin = %s WHERE id = %s", (role, is_admin, user_id))
                    else:
                        c.execute("UPDATE users SET role = ?, is_admin = ? WHERE id = ?", (role, is_admin, user_id))
                    conn.commit()
                    conn.close()
            except Exception as e:
                print(f"[WARN] Could not sync admin role to user: {e}")
            log_action(
                "ADMIN_LOGIN",
                f"Role: {role}",
                status="success",
                extras={"module": "admin_auth", "event": "verify_code"}
            )
            return jsonify({"success": True, "role": role, "redirect": "/admin/dashboard"})
    
    return jsonify({"success": False, "error": "Invalid code"}), 401

@admin_bp.route('/api/permissions')
@admin_required
def get_permissions():
    """Get current admin permissions"""
    admin = get_admin_session()
    return jsonify({
        "role": admin['role'],
        "permissions": ROLE_PERMISSIONS.get(admin['role'], []),
        "can_access_settings": has_permission('edit_settings')
    })

@admin_bp.route('/api/stats')
@admin_required
def get_stats():
    conn = None
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        
        def get_count(query, params=None):
            """Helper to get count from query, handling both db types"""
            try:
                if params:
                    c.execute(query, params)
                else:
                    c.execute(query)
                row = c.fetchone()
                if row is None:
                    return 0
                # Handle dict-like, sqlite3.Row, and tuple-like rows
                if isinstance(row, dict):
                    return row.get('count', 0) or 0
                if hasattr(row, 'keys'):
                    try:
                        return row['count'] or 0
                    except Exception:
                        try:
                            return row[0] or 0
                        except Exception:
                            return 0
                return row[0] or 0
            except Exception as e:
                print(f"[DEBUG] Query failed: {query}, error: {e}")
                return 0
        
        # Ensure tables exist first
        try:
            if db_type == 'postgres':
                c.execute("CREATE TABLE IF NOT EXISTS bans (id SERIAL PRIMARY KEY, user_id INTEGER UNIQUE, reason TEXT, banned_by TEXT, banned_at TIMESTAMP, expires_at TIMESTAMP)")
                c.execute("CREATE TABLE IF NOT EXISTS comment_restrictions (id SERIAL PRIMARY KEY, user_id INTEGER UNIQUE, reason TEXT, restricted_by TEXT, restricted_at TIMESTAMP, expires_at TIMESTAMP)")
                c.execute("CREATE TABLE IF NOT EXISTS verses (id SERIAL PRIMARY KEY, reference TEXT, text TEXT, translation TEXT, source TEXT, timestamp TEXT, book TEXT)")
                c.execute("CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, google_id TEXT UNIQUE, email TEXT, name TEXT, picture TEXT, created_at TEXT, is_admin INTEGER DEFAULT 0, is_banned BOOLEAN DEFAULT FALSE, ban_expires_at TIMESTAMP, ban_reason TEXT, role TEXT DEFAULT 'user')")
                c.execute("CREATE TABLE IF NOT EXISTS likes (id SERIAL PRIMARY KEY, user_id INTEGER, verse_id INTEGER, timestamp TEXT, UNIQUE(user_id, verse_id))")
                c.execute("CREATE TABLE IF NOT EXISTS saves (id SERIAL PRIMARY KEY, user_id INTEGER, verse_id INTEGER, timestamp TEXT, UNIQUE(user_id, verse_id))")
                c.execute("CREATE TABLE IF NOT EXISTS comments (id SERIAL PRIMARY KEY, user_id INTEGER, verse_id INTEGER, text TEXT, timestamp TEXT, google_name TEXT, google_picture TEXT, is_deleted INTEGER DEFAULT 0)")
                c.execute("CREATE TABLE IF NOT EXISTS community_messages (id SERIAL PRIMARY KEY, user_id INTEGER, text TEXT, timestamp TEXT, google_name TEXT, google_picture TEXT)")
                c.execute("CREATE TABLE IF NOT EXISTS comment_replies (id SERIAL PRIMARY KEY, parent_type TEXT NOT NULL, parent_id INTEGER NOT NULL, user_id INTEGER NOT NULL, text TEXT NOT NULL, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, google_name TEXT, google_picture TEXT, is_deleted INTEGER DEFAULT 0)")
                c.execute("CREATE TABLE IF NOT EXISTS daily_actions (id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL, action TEXT NOT NULL, verse_id INTEGER, event_date TEXT NOT NULL, timestamp TEXT)")
            else:
                c.execute("CREATE TABLE IF NOT EXISTS bans (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER UNIQUE, reason TEXT, banned_by TEXT, banned_at TIMESTAMP, expires_at TIMESTAMP)")
                c.execute("CREATE TABLE IF NOT EXISTS comment_restrictions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER UNIQUE, reason TEXT, restricted_by TEXT, restricted_at TIMESTAMP, expires_at TIMESTAMP)")
                c.execute("CREATE TABLE IF NOT EXISTS verses (id INTEGER PRIMARY KEY AUTOINCREMENT, reference TEXT, text TEXT, translation TEXT, source TEXT, timestamp TEXT, book TEXT)")
                c.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, google_id TEXT UNIQUE, email TEXT, name TEXT, picture TEXT, created_at TEXT, is_admin INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0, ban_expires_at TEXT, ban_reason TEXT, role TEXT DEFAULT 'user')")
                c.execute("CREATE TABLE IF NOT EXISTS likes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, verse_id INTEGER, timestamp TEXT, UNIQUE(user_id, verse_id))")
                c.execute("CREATE TABLE IF NOT EXISTS saves (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, verse_id INTEGER, timestamp TEXT, UNIQUE(user_id, verse_id))")
                c.execute("CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, verse_id INTEGER, text TEXT, timestamp TEXT, google_name TEXT, google_picture TEXT, is_deleted INTEGER DEFAULT 0)")
                c.execute("CREATE TABLE IF NOT EXISTS community_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, timestamp TEXT, google_name TEXT, google_picture TEXT)")
                c.execute("CREATE TABLE IF NOT EXISTS comment_replies (id INTEGER PRIMARY KEY AUTOINCREMENT, parent_type TEXT NOT NULL, parent_id INTEGER NOT NULL, user_id INTEGER NOT NULL, text TEXT NOT NULL, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, google_name TEXT, google_picture TEXT, is_deleted INTEGER DEFAULT 0)")
                c.execute("CREATE TABLE IF NOT EXISTS daily_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, action TEXT NOT NULL, verse_id INTEGER, event_date TEXT NOT NULL, timestamp TEXT)")
            conn.commit()
        except Exception as e:
            print(f"[DEBUG] Table creation warning: {e}")
            if db_type == 'postgres':
                conn.rollback()

        # Ensure soft-delete columns exist for comment counts
        try:
            if db_type == 'postgres':
                c.execute("ALTER TABLE comments ADD COLUMN IF NOT EXISTS is_deleted INTEGER DEFAULT 0")
                c.execute("ALTER TABLE comment_replies ADD COLUMN IF NOT EXISTS is_deleted INTEGER DEFAULT 0")
            else:
                try:
                    c.execute("SELECT is_deleted FROM comments LIMIT 1")
                except Exception:
                    c.execute("ALTER TABLE comments ADD COLUMN is_deleted INTEGER DEFAULT 0")
                try:
                    c.execute("SELECT is_deleted FROM comment_replies LIMIT 1")
                except Exception:
                    c.execute("ALTER TABLE comment_replies ADD COLUMN is_deleted INTEGER DEFAULT 0")
            conn.commit()
        except Exception as e:
            print(f"[DEBUG] Soft delete column warning: {e}")
        
        users = get_count("SELECT COUNT(*) as count FROM users")
        now_iso = datetime.now().isoformat()
        if db_type == 'postgres':
            bans = get_count("SELECT COUNT(*) as count FROM bans WHERE expires_at IS NULL OR expires_at > %s", (now_iso,))
            banned_users = get_count(
                "SELECT COUNT(*) as count FROM users WHERE is_banned = TRUE AND (ban_expires_at IS NULL OR ban_expires_at > %s)",
                (now_iso,)
            )
        else:
            bans = get_count("SELECT COUNT(*) as count FROM bans WHERE expires_at IS NULL OR expires_at > ?", (now_iso,))
            banned_users = get_count(
                "SELECT COUNT(*) as count FROM users WHERE is_banned = 1 AND (ban_expires_at IS NULL OR ban_expires_at > ?)",
                (now_iso,)
            )
        
        restricted = 0
        try:
            if db_type == 'postgres':
                restricted = get_count("SELECT COUNT(*) as count FROM comment_restrictions WHERE expires_at > NOW()")
            else:
                restricted = get_count("SELECT COUNT(*) as count FROM comment_restrictions WHERE expires_at > datetime('now')")
        except Exception as e:
            print(f"[DEBUG] Restricted count error: {e}")
        
        verses = get_count("SELECT COUNT(*) as count FROM verses")
        comments = get_count("SELECT COUNT(*) as count FROM comments WHERE COALESCE(is_deleted, 0) = 0")
        community_msgs = get_count("SELECT COUNT(*) as count FROM community_messages")
        replies = get_count("SELECT COUNT(*) as count FROM comment_replies WHERE COALESCE(is_deleted, 0) = 0")
        
        # Total comments activity = verse comments + community messages + replies
        total_comments = comments + community_msgs + replies
        
        print(f"[DEBUG] Admin stats: users={users}, bans={bans}, restricted={restricted}, verses={verses}, comments={comments}, community={community_msgs}, replies={replies}, total={total_comments}")
        
        if conn:
            conn.close()
        
        admin = get_admin_session()
        return jsonify({
            "users": users,
            "bans": max(bans, banned_users),
            "restricted": restricted,
            "views": 0,  # Views column doesn't exist yet
            "verses": verses,
            "comments": total_comments,
            "role": admin['role'],
            "level": admin['level']
        })
    except Exception as e:
        print(f"[ERROR] Stats: {e}")
        import traceback
        traceback.print_exc()
        if conn:
            try:
                conn.close()
            except:
                pass
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/users')
@admin_required
def get_users():
    try:
        conn, db_type = get_db()
        c = conn.cursor()

        search = (request.args.get('q') or '').strip()
        role = (request.args.get('role') or '').strip()
        status = (request.args.get('status') or '').strip().lower()

        where = []
        params = []

        if search:
            if db_type == 'postgres':
                where.append("(COALESCE(name, '') ILIKE %s OR COALESCE(email, '') ILIKE %s)")
            else:
                where.append("(LOWER(COALESCE(name, '')) LIKE ? OR LOWER(COALESCE(email, '')) LIKE ?)")
            token = f"%{search.lower()}%"
            params.extend([token, token] if db_type != 'postgres' else [f"%{search}%", f"%{search}%"])

        def normalize_role_value(value):
            r = (value or '').strip().lower()
            if r in ('co-owner', 'co owner', 'coowner'):
                return 'co_owner'
            if r in ('owner', 'host', 'mod'):
                return r
            return r or 'user'

        if role:
            if db_type == 'postgres':
                where.append("LOWER(COALESCE(role, 'user')) = LOWER(%s)")
            else:
                where.append("LOWER(COALESCE(role, 'user')) = LOWER(?)")
            params.append(role)

        if status == 'banned':
            where.append("is_banned = TRUE" if db_type == 'postgres' else "is_banned = 1")
        elif status == 'active':
            where.append("(is_banned IS NULL OR is_banned = FALSE)" if db_type == 'postgres' else "(is_banned IS NULL OR is_banned = 0)")

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        query = f"SELECT id, name, email, role, is_admin, is_banned, created_at FROM users{where_sql} ORDER BY id DESC"
        c.execute(query, tuple(params))
        rows = c.fetchall()
        
        users = []
        for row in rows:
            role_val = normalize_role_value(row[3] or "user")
            users.append({
                "id": row[0],
                "name": row[1] or "Unknown",
                "email": row[2] or "No email",
                "role": role_val,
                "is_admin": bool(row[4]),
                "is_banned": bool(row[5]),
                "created_at": row[6] or "Unknown"
            })
        conn.close()
        return jsonify(users)
    except Exception as e:
        print(f"[ERROR] Users: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/bans')
@admin_required
def get_bans():
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        
        # Create table with appropriate syntax
        if db_type == 'postgres':
            c.execute("""
                CREATE TABLE IF NOT EXISTS bans (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER UNIQUE,
                    reason TEXT,
                    banned_by TEXT,
                    banned_at TIMESTAMP,
                    expires_at TIMESTAMP,
                    ip_address TEXT
                )
            """)
            # Ensure ip_address column exists for legacy databases
            try:
                c.execute("ALTER TABLE bans ADD COLUMN IF NOT EXISTS ip_address TEXT")
            except Exception:
                pass
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS bans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE,
                    reason TEXT,
                    banned_by TEXT,
                    banned_at TIMESTAMP,
                    expires_at TIMESTAMP,
                    ip_address TEXT
                )
            """)
            # Ensure ip_address column exists for legacy databases
            try:
                c.execute("SELECT ip_address FROM bans LIMIT 1")
            except Exception:
                try:
                    c.execute("ALTER TABLE bans ADD COLUMN ip_address TEXT")
                except Exception:
                    pass
        
        c.execute("""
            SELECT b.id, b.user_id, b.reason, b.banned_by, b.banned_at, b.expires_at,
                   b.ip_address, u.name, u.email
            FROM bans b
            LEFT JOIN users u ON b.user_id = u.id
            ORDER BY b.banned_at DESC
        """)
        
        rows = c.fetchall()
        
        bans = []
        for row in rows:
            bans.append({
                "id": row[0],
                "user_id": row[1],
                "reason": row[2] or "No reason",
                "banned_by": row[3] or "Unknown",
                "banned_at": row[4],
                "expires_at": row[5],
                "ip_address": row[6] or "Unknown",
                "user_name": row[7] or "Unknown",
                "user_email": row[8] or "No email"
            })
        # Include users marked as banned even if bans table is empty/out of sync
        try:
            if db_type == 'postgres':
                c.execute("""
                    SELECT id, name, email, ban_reason, ban_expires_at
                    FROM users
                    WHERE is_banned = TRUE
                    AND id NOT IN (SELECT user_id FROM bans)
                """)
            else:
                c.execute("""
                    SELECT id, name, email, ban_reason, ban_expires_at
                    FROM users
                    WHERE is_banned = 1
                    AND id NOT IN (SELECT user_id FROM bans)
                """)
            extra_rows = c.fetchall()
            for row in extra_rows:
                bans.append({
                    "id": None,
                    "user_id": row[0],
                    "reason": row[3] or "No reason",
                    "banned_by": "system",
                    "banned_at": None,
                    "expires_at": row[4],
                    "ip_address": "Unknown",
                    "user_name": row[1] or "Unknown",
                    "user_email": row[2] or "No email"
                })
        except Exception:
            pass
        conn.close()
        return jsonify(bans)
    except Exception as e:
        print(f"[ERROR] Bans: {e}")
        return jsonify({"error": str(e)}), 500

# Comment Restrictions (Host+ can use)
@admin_bp.route('/api/restrictions')
@admin_required
@require_permission('restrict_comments')
def get_restrictions():
    """Get all active comment restrictions"""
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        
        # Ensure table exists with appropriate syntax
        if db_type == 'postgres':
            c.execute("""
                CREATE TABLE IF NOT EXISTS comment_restrictions (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER UNIQUE,
                    reason TEXT,
                    restricted_by TEXT,
                    restricted_at TIMESTAMP,
                    expires_at TIMESTAMP
                )
            """)
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS comment_restrictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE,
                    reason TEXT,
                    restricted_by TEXT,
                    restricted_at TIMESTAMP,
                    expires_at TIMESTAMP
                )
            """)
        
        now = datetime.now().isoformat()
        if db_type == 'postgres':
            c.execute("""
                SELECT r.id, r.user_id, r.reason, r.restricted_by, r.restricted_at, r.expires_at,
                       u.name, u.email
                FROM comment_restrictions r
                LEFT JOIN users u ON r.user_id = u.id
                WHERE r.expires_at > %s
                ORDER BY r.restricted_at DESC
            """, (now,))
        else:
            c.execute("""
                SELECT r.id, r.user_id, r.reason, r.restricted_by, r.restricted_at, r.expires_at,
                       u.name, u.email
                FROM comment_restrictions r
                LEFT JOIN users u ON r.user_id = u.id
                WHERE r.expires_at > ?
                ORDER BY r.restricted_at DESC
            """, (now,))
        
        rows = c.fetchall()
        conn.close()
        
        restrictions = []
        for row in rows:
            restrictions.append({
                "id": row[0],
                "user_id": row[1],
                "reason": row[2] or "No reason",
                "restricted_by": row[3] or "Unknown",
                "restricted_at": row[4],
                "expires_at": row[5],
                "user_name": row[6] or "Unknown",
                "user_email": row[7] or "No email"
            })
        return jsonify(restrictions)
    except Exception as e:
        print(f"[ERROR] Restrictions: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/users/<int:user_id>/restrict', methods=['POST'])
@admin_required
@require_permission('restrict_comments')
def restrict_user(user_id):
    """Restrict user from commenting"""
    data = request.get_json()
    hours = data.get('hours', 24)
    reason = data.get('reason', 'No reason provided')
    
    print(f"[DEBUG] restrict_user called: user_id={user_id}, hours={hours}, reason={reason}")
    
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        
        print(f"[DEBUG] db_type={db_type}")
        
        admin = get_admin_session()
        
        # Get user info
        if db_type == 'postgres':
            c.execute("SELECT name, role FROM users WHERE id = %s", (user_id,))
        else:
            c.execute("SELECT name, role FROM users WHERE id = ?", (user_id,))
        row = c.fetchone()
        if not row:
            print(f"[DEBUG] User {user_id} not found")
            conn.close()
            return jsonify({"error": "User not found"}), 404
        
        user_name, user_role = row[0] or "Unknown", row[1] or "user"
        print(f"[DEBUG] Found user: {user_name}, role: {user_role}")
        
        # Can't restrict higher or equal roles
        if not can_modify_role(admin['role'], user_role):
            conn.close()
            return jsonify({"error": "Cannot restrict this user"}), 403
        
        # Calculate expiration
        expires_at = datetime.now() + timedelta(hours=hours)
        now = datetime.now().isoformat()
        expires_iso = expires_at.isoformat()
        
        print(f"[DEBUG] Creating restriction: now={now}, expires={expires_iso}")
        
        # Create table with appropriate syntax for database type
        if db_type == 'postgres':
            c.execute("""
                CREATE TABLE IF NOT EXISTS comment_restrictions (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER UNIQUE,
                    reason TEXT,
                    restricted_by TEXT,
                    restricted_at TIMESTAMP,
                    expires_at TIMESTAMP
                )
            """)
            # Use INSERT ON CONFLICT for PostgreSQL
            c.execute("""
                INSERT INTO comment_restrictions (user_id, reason, restricted_by, restricted_at, expires_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    reason = EXCLUDED.reason,
                    restricted_by = EXCLUDED.restricted_by,
                    restricted_at = EXCLUDED.restricted_at,
                    expires_at = EXCLUDED.expires_at
            """, (user_id, reason, admin['role'], now, expires_iso))
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS comment_restrictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE,
                    reason TEXT,
                    restricted_by TEXT,
                    restricted_at TIMESTAMP,
                    expires_at TIMESTAMP
                )
            """)
            c.execute("""
                INSERT OR REPLACE INTO comment_restrictions (user_id, reason, restricted_by, restricted_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, reason, admin['role'], now, expires_iso))
        
        conn.commit()
        
        # Verify the restriction was created
        if db_type == 'postgres':
            c.execute("SELECT user_id, reason, expires_at FROM comment_restrictions WHERE user_id = %s", (user_id,))
        else:
            c.execute("SELECT user_id, reason, expires_at FROM comment_restrictions WHERE user_id = ?", (user_id,))
        verify = c.fetchone()
        print(f"[DEBUG] Verification query result: {verify}")
        
        # Log the action BEFORE closing connection
        log_action(
            "RESTRICT_COMMENTS",
            f"Restricted {user_name} ({user_id}) for {hours}h: {reason}",
            target_user_id=user_id,
            status="success",
            extras={"reason": reason, "duration": f"{hours}h", "module": "comments"},
            target={"user_id": user_id, "name": user_name, "role": user_role}
        )
        
        conn.close()
        print(f"[DEBUG] Restriction successful for user {user_id}")
        return jsonify({"success": True, "hours": hours})
    except Exception as e:
        print(f"[ERROR] Restrict: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/users/<int:user_id>/restrict', methods=['DELETE'])
@admin_required
@require_permission('restrict_comments')
def remove_restriction(user_id):
    """Remove comment restriction from user"""
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        
        if db_type == 'postgres':
            c.execute("DELETE FROM comment_restrictions WHERE user_id = %s", (user_id,))
        else:
            c.execute("DELETE FROM comment_restrictions WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        
        log_action(
            "UNRESTRICT",
            f"Removed comment restriction from user {user_id}",
            target_user_id=user_id,
            status="success",
            extras={"module": "comments", "event": "restriction_removed"}
        )
        return jsonify({"success": True})
    except Exception as e:
        print(f"[ERROR] Unrestrict: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/comments')
@admin_required
@require_permission('delete_comments')
def get_comments():
    """Get all comments and community messages for moderation"""
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        comment_type = (request.args.get('type') or 'all').strip().lower()
        
        print(f"[DEBUG] Getting comments, db_type={db_type}")
        
        all_items = []
        
        def _reaction_counts(item_type, item_id):
            result = {"heart": 0, "pray": 0, "cross": 0}
            try:
                if db_type == 'postgres':
                    c.execute("""
                        SELECT reaction, COUNT(*) AS cnt
                        FROM comment_reactions
                        WHERE item_type = %s AND item_id = %s
                        GROUP BY reaction
                    """, (item_type, item_id))
                else:
                    c.execute("""
                        SELECT reaction, COUNT(*) AS cnt
                        FROM comment_reactions
                        WHERE item_type = ? AND item_id = ?
                        GROUP BY reaction
                    """, (item_type, item_id))
                rows = c.fetchall()
                for rr in rows:
                    rr = _row_to_dict(rr)
                    key = (rr.get('reaction') if hasattr(rr, 'keys') else rr[0]) or ''
                    cnt = rr.get('cnt') if hasattr(rr, 'keys') else rr[1]
                    key = str(key).lower()
                    if key in result:
                        result[key] = int(cnt or 0)
            except Exception:
                pass
            return result

        def _reply_count(item_type, item_id):
            try:
                if db_type == 'postgres':
                    c.execute("""
                        SELECT COUNT(*) AS cnt FROM comment_replies
                        WHERE parent_type = %s AND parent_id = %s AND COALESCE(is_deleted, 0) = 0
                    """, (item_type, item_id))
                else:
                    c.execute("""
                        SELECT COUNT(*) AS cnt FROM comment_replies
                        WHERE parent_type = ? AND parent_id = ? AND COALESCE(is_deleted, 0) = 0
                    """, (item_type, item_id))
                row = c.fetchone()
                row = _row_to_dict(row)
                return int((row.get('cnt') if hasattr(row, 'keys') else row[0]) if row else 0)
            except Exception:
                return 0

        def _replies(item_type, item_id):
            try:
                if db_type == 'postgres':
                    c.execute("""
                        SELECT text, timestamp, google_name
                        FROM comment_replies
                        WHERE parent_type = %s AND parent_id = %s AND COALESCE(is_deleted, 0) = 0
                        ORDER BY timestamp ASC
                        LIMIT 20
                    """, (item_type, item_id))
                else:
                    c.execute("""
                        SELECT text, timestamp, google_name
                        FROM comment_replies
                        WHERE parent_type = ? AND parent_id = ? AND COALESCE(is_deleted, 0) = 0
                        ORDER BY timestamp ASC
                        LIMIT 20
                    """, (item_type, item_id))
                rows = c.fetchall()
                result = []
                for rr in rows:
                    rr = _row_to_dict(rr)
                    if hasattr(rr, 'keys'):
                        result.append({
                            "text": rr.get('text') or "",
                            "timestamp": rr.get('timestamp'),
                            "user_name": rr.get('google_name') or "Anonymous"
                        })
                    else:
                        result.append({
                            "text": rr[0] or "",
                            "timestamp": rr[1],
                            "user_name": rr[2] or "Anonymous"
                        })
                return result
            except Exception:
                return []

        # Get verse comments
        if comment_type in ('all', 'comment'):
            try:
                if db_type == 'postgres':
                    c.execute("""
                        SELECT id, verse_id, text, timestamp, google_name, user_id, 'comment' as type
                        FROM comments
                        WHERE COALESCE(is_deleted, 0) = 0
                        ORDER BY timestamp DESC
                        LIMIT 100
                    """)
                else:
                    c.execute("""
                        SELECT id, verse_id, text, timestamp, google_name, user_id, 'comment' as type
                        FROM comments
                        WHERE COALESCE(is_deleted, 0) = 0
                        ORDER BY timestamp DESC
                        LIMIT 100
                    """)
                
                rows = c.fetchall()
                print(f"[DEBUG] Found {len(rows)} verse comments")
                
                for row in rows:
                    row_id = row[0]
                    all_items.append({
                        "id": row_id,
                        "verse_id": row[1],
                        "text": row[2] or "",
                        "timestamp": row[3],
                        "google_name": row[4] or "Anonymous",
                        "user_name": row[4] or "Anonymous",
                        "user_id": row[5],
                        "type": row[6],
                        "email": "No email",
                        "reactions": _reaction_counts("comment", row_id),
                        "reply_count": _reply_count("comment", row_id),
                        "replies": _replies("comment", row_id)
                    })
            except Exception as e:
                print(f"[ERROR] Getting verse comments: {e}")
        
        # Get community messages
        if comment_type in ('all', 'community'):
            try:
                if db_type == 'postgres':
                    c.execute("""
                        SELECT id, NULL as verse_id, text, timestamp, google_name, user_id, 'community' as type
                        FROM community_messages
                        ORDER BY timestamp DESC
                        LIMIT 100
                    """)
                else:
                    c.execute("""
                        SELECT id, NULL as verse_id, text, timestamp, google_name, user_id, 'community' as type
                        FROM community_messages
                        ORDER BY timestamp DESC
                        LIMIT 100
                    """)
                
                rows = c.fetchall()
                print(f"[DEBUG] Found {len(rows)} community messages")
                
                for row in rows:
                    row_id = row[0]
                    all_items.append({
                        "id": row_id,
                        "verse_id": row[1],
                        "text": row[2] or "",
                        "timestamp": row[3],
                        "google_name": row[4] or "Anonymous",
                        "user_name": row[4] or "Anonymous",
                        "user_id": row[5],
                        "type": row[6],
                        "email": "No email",
                        "reactions": _reaction_counts("community", row_id),
                        "reply_count": _reply_count("community", row_id),
                        "replies": _replies("community", row_id)
                    })
            except Exception as e:
                print(f"[ERROR] Getting community messages: {e}")
        
        conn.close()
        
        # Sort by timestamp descending
        all_items.sort(key=lambda x: x['timestamp'] if x['timestamp'] else '', reverse=True)
        
        print(f"[DEBUG] Returning {len(all_items)} total items")
        return jsonify(all_items[:100])  # Limit to 100 total
    except Exception as e:
        print(f"[ERROR] Comments: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/comments/<int:comment_id>', methods=['DELETE'])
@admin_required
@require_permission('delete_comments')
def delete_comment(comment_id):
    """Soft delete a comment"""
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        comment_type = (request.args.get('type') or 'comment').strip().lower()

        # Ensure replies table has is_deleted column
        try:
            if db_type == 'postgres':
                c.execute("ALTER TABLE comment_replies ADD COLUMN IF NOT EXISTS is_deleted INTEGER DEFAULT 0")
            else:
                c.execute("SELECT is_deleted FROM comment_replies LIMIT 1")
        except Exception:
            try:
                c.execute("ALTER TABLE comment_replies ADD COLUMN is_deleted INTEGER DEFAULT 0")
            except Exception:
                pass
        
        if comment_type == 'community':
            if db_type == 'postgres':
                c.execute("DELETE FROM community_messages WHERE id = %s", (comment_id,))
            else:
                c.execute("DELETE FROM community_messages WHERE id = ?", (comment_id,))
            try:
                if db_type == 'postgres':
                    c.execute("UPDATE comment_replies SET is_deleted = 1 WHERE parent_type = %s AND parent_id = %s",
                              ('community', comment_id))
                else:
                    c.execute("UPDATE comment_replies SET is_deleted = 1 WHERE parent_type = ? AND parent_id = ?",
                              ('community', comment_id))
            except Exception:
                pass
        else:
            if db_type == 'postgres':
                c.execute("UPDATE comments SET is_deleted = 1 WHERE id = %s", (comment_id,))
            else:
                c.execute("UPDATE comments SET is_deleted = 1 WHERE id = ?", (comment_id,))
            try:
                if db_type == 'postgres':
                    c.execute("UPDATE comment_replies SET is_deleted = 1 WHERE parent_type = %s AND parent_id = %s",
                              ('comment', comment_id))
                else:
                    c.execute("UPDATE comment_replies SET is_deleted = 1 WHERE parent_type = ? AND parent_id = ?",
                              ('comment', comment_id))
            except Exception:
                pass
        conn.commit()
        conn.close()
        
        log_action(
            "DELETE_COMMENT",
            f"Deleted {comment_type} {comment_id}",
            status="success",
            extras={"comment_type": comment_type, "comment_id": comment_id}
        )
        return jsonify({"success": True})
    except Exception as e:
        print(f"[ERROR] Delete comment: {e}")
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/users/<int:user_id>/ban', methods=['POST'])
@admin_required
@require_permission('ban')
def ban_user(user_id):
    admin = get_admin_session()
    data = request.get_json()
    banned = data.get('banned', True)
    reason = data.get('reason', 'No reason provided')
    duration = data.get('duration', 'permanent')
    
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        
        # Create table with appropriate syntax
        if db_type == 'postgres':
            c.execute("""
                CREATE TABLE IF NOT EXISTS bans (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER UNIQUE,
                    reason TEXT,
                    banned_by TEXT,
                    banned_at TIMESTAMP,
                    expires_at TIMESTAMP
                )
            """)
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS bans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE,
                    reason TEXT,
                    banned_by TEXT,
                    banned_at TIMESTAMP,
                    expires_at TIMESTAMP
                )
            """)
        
        if db_type == 'postgres':
            c.execute("SELECT role, name FROM users WHERE id = %s", (user_id,))
        else:
            c.execute("SELECT role, name FROM users WHERE id = ?", (user_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "User not found"}), 404
        
        target_role = row[0] or "user"
        user_name = row[1] or "Unknown"
        
        if not can_modify_role(admin['role'], target_role):
            conn.close()
            return jsonify({"error": "Cannot ban this user"}), 403

        # Ensure ban columns exist on users table for legacy DBs
        try:
            if db_type == 'postgres':
                c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ban_expires_at TIMESTAMP")
                c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ban_reason TEXT")
            else:
                try:
                    c.execute("SELECT ban_expires_at FROM users LIMIT 1")
                except Exception:
                    c.execute("ALTER TABLE users ADD COLUMN ban_expires_at TEXT")
                try:
                    c.execute("SELECT ban_reason FROM users LIMIT 1")
                except Exception:
                    c.execute("ALTER TABLE users ADD COLUMN ban_reason TEXT")
        except Exception:
            pass
        
        # Get the user's IP address from signup logs for IP-based ban tracking
        banned_ip = None
        try:
            if db_type == 'postgres':
                c.execute("SELECT signup_ip FROM user_signup_logs WHERE user_id = %s", (user_id,))
            else:
                c.execute("SELECT signup_ip FROM user_signup_logs WHERE user_id = ?", (user_id,))
            ip_row = c.fetchone()
            if ip_row:
                banned_ip = ip_row[0] if ip_row else None
        except Exception:
            pass  # Table might not exist yet
        
        if banned:
            expires_at = None
            if duration != 'permanent':
                now = datetime.now()
                if duration.endswith('m'):
                    expires_at = now + timedelta(minutes=int(duration[:-1]))
                elif duration.endswith('h'):
                    expires_at = now + timedelta(hours=int(duration[:-1]))
                elif duration.endswith('d'):
                    expires_at = now + timedelta(days=int(duration[:-1]))
                elif duration.endswith('w'):
                    expires_at = now + timedelta(weeks=int(duration[:-1]))
                elif duration.endswith('mo'):
                    expires_at = now + timedelta(days=int(duration[:-2]) * 30)
                expires_at = expires_at.isoformat() if expires_at else None
            
            if db_type == 'postgres':
                c.execute(
                    "UPDATE users SET is_banned = TRUE, ban_expires_at = %s, ban_reason = %s WHERE id = %s",
                    (expires_at, reason, user_id)
                )
                c.execute("""
                    INSERT INTO bans (user_id, reason, banned_by, banned_at, expires_at, ip_address)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET
                        reason = EXCLUDED.reason,
                        banned_by = EXCLUDED.banned_by,
                        banned_at = EXCLUDED.banned_at,
                        expires_at = EXCLUDED.expires_at,
                        ip_address = EXCLUDED.ip_address
                """, (user_id, reason, admin['role'], datetime.now().isoformat(), expires_at, banned_ip))
            else:
                c.execute(
                    "UPDATE users SET is_banned = 1, ban_expires_at = ?, ban_reason = ? WHERE id = ?",
                    (expires_at, reason, user_id)
                )
                c.execute("""
                    INSERT OR REPLACE INTO bans (user_id, reason, banned_by, banned_at, expires_at, ip_address)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (user_id, reason, admin['role'], datetime.now().isoformat(), expires_at, banned_ip))
            
            log_action(
                "BAN",
                f"Banned {user_name} ({user_id}) for {duration}: {reason}",
                target_user_id=user_id,
                status="success",
                extras={"reason": reason, "duration": duration, "expires_at": expires_at, "module": "moderation"},
                target={"user_id": user_id, "name": user_name, "role": target_role}
            )
        else:
            if db_type == 'postgres':
                c.execute(
                    "UPDATE users SET is_banned = FALSE, ban_expires_at = NULL, ban_reason = NULL WHERE id = %s",
                    (user_id,)
                )
                c.execute("DELETE FROM bans WHERE user_id = %s", (user_id,))
            else:
                c.execute(
                    "UPDATE users SET is_banned = 0, ban_expires_at = NULL, ban_reason = NULL WHERE id = ?",
                    (user_id,)
                )
                c.execute("DELETE FROM bans WHERE user_id = ?", (user_id,))
            log_action(
                "UNBAN",
                f"Unbanned {user_name} ({user_id})",
                target_user_id=user_id,
                status="success",
                extras={"module": "moderation", "event": "ban_removed"},
                target={"user_id": user_id, "name": user_name, "role": target_role}
            )
        
        conn.commit()
        conn.close()
        
        return jsonify({"success": True, "banned": banned})
    except Exception as e:
        print(f"[ERROR] Ban: {e}")
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/users/<int:user_id>/role', methods=['POST'])
@admin_required
@require_permission('change_roles')
def update_user_role(user_id):
    admin = get_admin_session()
    data = request.get_json()
    new_role = data.get('role')
    
    if not new_role or new_role not in ROLE_HIERARCHY:
        return jsonify({"error": "Invalid role"}), 400
    
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        
        if db_type == 'postgres':
            c.execute("SELECT role FROM users WHERE id = %s", (user_id,))
        else:
            c.execute("SELECT role FROM users WHERE id = ?", (user_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "User not found"}), 404
        
        current_role = row[0] or "user"
        
        if not can_modify_role(admin['role'], current_role):
            conn.close()
            return jsonify({"error": "Cannot modify this user"}), 403
        
        if not can_modify_role(admin['role'], new_role):
            conn.close()
            return jsonify({"error": "Cannot assign this role"}), 403
        
        is_admin = 1 if ROLE_HIERARCHY[new_role] > 0 else 0
        if db_type == 'postgres':
            c.execute("UPDATE users SET role = %s, is_admin = %s WHERE id = %s", 
                     (new_role, is_admin, user_id))
        else:
            c.execute("UPDATE users SET role = ?, is_admin = ? WHERE id = ?", 
                     (new_role, is_admin, user_id))
        conn.commit()
        conn.close()
        
        log_action(
            "UPDATE_ROLE",
            f"User {user_id} to {new_role}",
            target_user_id=user_id,
            status="success",
            extras={"new_role": new_role, "previous_role": current_role, "module": "users"}
        )
        return jsonify({"success": True})
    except Exception as e:
        print(f"[ERROR] Update role: {e}")
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/audit-logs')
@admin_required
def get_audit_logs():
    conn = None
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        _ensure_audit_logs_schema(conn, c, db_type)
        logs, _ = _read_audit_logs(c, db_type, limit=100, offset=0, action=None)
        conn.close()
        return jsonify(logs)
    except Exception as e:
        print(f"[ERROR] Audit logs: {e}")
        if conn:
            try:
                conn.rollback()
                conn.close()
            except:
                pass
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/audits')
@admin_required
@require_permission('view_audit')
def get_audits():
    """Paginated audits API for admin audits page."""
    conn = None
    try:
        page = max(1, int(request.args.get('page', 1)))
        per_page = min(200, max(1, int(request.args.get('per_page', 50))))
        offset = (page - 1) * per_page

        action = request.args.get('action', 'all')
        action_map = {
            'user_banned': 'BAN',
            'user_unbanned': 'UNBAN',
            'user_updated': 'UPDATE_ROLE',
            'admin_verified': 'ADMIN_LOGIN',
            'system_settings_updated': 'UPDATE_SETTINGS'
        }
        normalized_action = action_map.get(action, action)

        conn, db_type = get_db()
        c = conn.cursor()
        _ensure_audit_logs_schema(conn, c, db_type)
        logs, total = _read_audit_logs(c, db_type, limit=per_page, offset=offset, action=normalized_action)
        conn.close()

        pages = max(1, (total + per_page - 1) // per_page)
        return jsonify({
            "logs": logs,
            "page": page,
            "pages": pages,
            "per_page": per_page,
            "total": total
        })
    except Exception as e:
        print(f"[ERROR] Audits API: {e}")
        if conn:
            try:
                conn.rollback()
                conn.close()
            except:
                pass
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/recent-activity')
@admin_required
def get_recent_activity():
    """Get recent activity for dashboard (last 10 actions)"""
    conn = None
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        _ensure_audit_logs_schema(conn, c, db_type)
        logs, _ = _read_audit_logs(c, db_type, limit=10, offset=0, action=None)
        conn.close()
        return jsonify(logs)
    except Exception as e:
        print(f"[ERROR] Recent activity: {e}")
        if conn:
            try:
                conn.rollback()
                conn.close()
            except:
                pass
        return jsonify([]), 200  # Return empty array on error

@admin_bp.route('/api/settings', methods=['GET'])
@admin_required
@require_permission('view_settings')
def get_settings():
    admin = get_admin_session()
    maintenance_mode = os.environ.get('MAINTENANCE_MODE', 'false')
    conn = None
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        if db_type == 'postgres':
            c.execute("SELECT value FROM system_settings WHERE key = %s", ('maintenance_mode',))
        else:
            c.execute("SELECT value FROM system_settings WHERE key = ?", ('maintenance_mode',))
        row = _row_to_dict(c.fetchone())
        if row:
            maintenance_mode = (row.get('value') if hasattr(row, 'keys') else row[0]) or maintenance_mode
        conn.close()
    except Exception:
        if conn:
            try:
                conn.close()
            except:
                pass
    
    return jsonify({
        "site_name": os.environ.get('SITE_NAME', 'AI.Bible'),
        "maintenance_mode": maintenance_mode,
        "codes": ROLE_CODES,
        "role": admin['role'],
        "is_owner": admin['role'] == 'owner',
        "can_edit": has_permission('edit_settings')
    })

@admin_bp.route('/api/check-session')
def check_session():
    admin = get_admin_session()
    if admin:
        try:
            user_id = session.get('user_id')
            if user_id:
                conn, db_type = get_db()
                c = conn.cursor()
                if db_type == 'postgres':
                    c.execute("SELECT role FROM users WHERE id = %s", (user_id,))
                else:
                    c.execute("SELECT role FROM users WHERE id = ?", (user_id,))
                row = c.fetchone()
                db_role = None
                if row:
                    try:
                        db_role = row['role']
                    except Exception:
                        db_role = row[0]
                admin_role = (admin.get('role') or 'user').strip().lower()
                db_role_norm = (db_role or 'user').strip().lower()
                if ROLE_HIERARCHY.get(admin_role, 0) > ROLE_HIERARCHY.get(db_role_norm, 0):
                    is_admin = 1 if ROLE_HIERARCHY.get(admin_role, 0) > 0 else 0
                    if db_type == 'postgres':
                        c.execute("UPDATE users SET role = %s, is_admin = %s WHERE id = %s", (admin_role, is_admin, user_id))
                    else:
                        c.execute("UPDATE users SET role = ?, is_admin = ? WHERE id = ?", (admin_role, is_admin, user_id))
                    conn.commit()
                conn.close()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
        return jsonify({"logged_in": True, "role": admin['role']})
    return jsonify({"logged_in": False}), 401

def _dispatch_announcement_row(c, db_type, announcement_row):
    """Insert notification rows for target users and mark announcement sent."""
    announcement_row = _row_to_dict(announcement_row)
    if hasattr(announcement_row, 'keys'):
        ann_id = announcement_row.get('id')
        title = announcement_row.get('title') or 'Announcement'
        message = announcement_row.get('message') or ''
        is_global = int(announcement_row.get('is_global') or 0) == 1
        target_user_id = announcement_row.get('target_user_id')
    else:
        ann_id, title, message, is_global, target_user_id = announcement_row[0], announcement_row[1], announcement_row[2], (announcement_row[3] == 1), announcement_row[4]

    now_iso = _iso_now()
    created = 0

    if is_global:
        if db_type == 'postgres':
            c.execute("SELECT id FROM users")
        else:
            c.execute("SELECT id FROM users")
        users = c.fetchall()
        user_ids = []
        for u in users:
            u = _row_to_dict(u)
            uid = u.get('id') if hasattr(u, 'keys') else u[0]
            if uid is not None:
                user_ids.append(uid)
        for uid in user_ids:
            if db_type == 'postgres':
                c.execute("""
                    INSERT INTO user_notifications (user_id, title, message, notif_type, source, is_read, created_at, sent_at)
                    VALUES (%s, %s, %s, %s, %s, 0, %s, %s)
                """, (uid, title, message, 'announcement', 'admin', now_iso, now_iso))
            else:
                c.execute("""
                    INSERT INTO user_notifications (user_id, title, message, notif_type, source, is_read, created_at, sent_at)
                    VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """, (uid, title, message, 'announcement', 'admin', now_iso, now_iso))
            created += 1
    elif target_user_id is not None:
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO user_notifications (user_id, title, message, notif_type, source, is_read, created_at, sent_at)
                VALUES (%s, %s, %s, %s, %s, 0, %s, %s)
            """, (target_user_id, title, message, 'direct_message', 'admin', now_iso, now_iso))
        else:
            c.execute("""
                INSERT INTO user_notifications (user_id, title, message, notif_type, source, is_read, created_at, sent_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """, (target_user_id, title, message, 'direct_message', 'admin', now_iso, now_iso))
        created = 1

    if db_type == 'postgres':
        c.execute("UPDATE admin_announcements SET status = %s, sent_at = %s WHERE id = %s", ('sent', now_iso, ann_id))
    else:
        c.execute("UPDATE admin_announcements SET status = ?, sent_at = ? WHERE id = ?", ('sent', now_iso, ann_id))

    return created

@admin_bp.route('/api/insights')
@admin_required
@require_permission('view_audit')
def get_admin_insights():
    conn = None
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        _ensure_admin_feature_tables(conn, c, db_type)
        _ensure_daily_actions_schema(c, db_type)
        if db_type == 'postgres':
            c.execute("CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, google_id TEXT UNIQUE, email TEXT, name TEXT, picture TEXT, created_at TEXT, is_admin INTEGER DEFAULT 0, is_banned BOOLEAN DEFAULT FALSE, ban_expires_at TIMESTAMP, ban_reason TEXT, role TEXT DEFAULT 'user')")
            c.execute("CREATE TABLE IF NOT EXISTS likes (id SERIAL PRIMARY KEY, user_id INTEGER, verse_id INTEGER, timestamp TEXT, UNIQUE(user_id, verse_id))")
            c.execute("CREATE TABLE IF NOT EXISTS saves (id SERIAL PRIMARY KEY, user_id INTEGER, verse_id INTEGER, timestamp TEXT, UNIQUE(user_id, verse_id))")
            c.execute("CREATE TABLE IF NOT EXISTS daily_actions (id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL, action TEXT NOT NULL, verse_id INTEGER, event_date TEXT NOT NULL, timestamp TEXT)")
        else:
            c.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, google_id TEXT UNIQUE, email TEXT, name TEXT, picture TEXT, created_at TEXT, is_admin INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0, ban_expires_at TEXT, ban_reason TEXT, role TEXT DEFAULT 'user')")
            c.execute("CREATE TABLE IF NOT EXISTS likes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, verse_id INTEGER, timestamp TEXT, UNIQUE(user_id, verse_id))")
            c.execute("CREATE TABLE IF NOT EXISTS saves (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, verse_id INTEGER, timestamp TEXT, UNIQUE(user_id, verse_id))")
            c.execute("CREATE TABLE IF NOT EXISTS daily_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, action TEXT NOT NULL, verse_id INTEGER, event_date TEXT NOT NULL, timestamp TEXT)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        now = datetime.now()
        active_cutoff = now - timedelta(minutes=5)
        retention_1d_cutoff = now - timedelta(days=1)
        retention_7d_cutoff = now - timedelta(days=7)
        retention_30d_cutoff = now - timedelta(days=30)

        # Process due scheduled announcements while loading insights.
        now_iso = _iso_now()
        if db_type == 'postgres':
            c.execute("""
                SELECT id, title, message, is_global, target_user_id
                FROM admin_announcements
                WHERE status = %s AND scheduled_for IS NOT NULL AND scheduled_for <= %s
                ORDER BY scheduled_for ASC
            """, ('scheduled', now_iso))
        else:
            c.execute("""
                SELECT id, title, message, is_global, target_user_id
                FROM admin_announcements
                WHERE status = ? AND scheduled_for IS NOT NULL AND scheduled_for <= ?
                ORDER BY scheduled_for ASC
            """, ('scheduled', now_iso))
        due_rows = c.fetchall()
        for row in due_rows:
            _dispatch_announcement_row(c, db_type, row)

        # Active users now from presence pings.
        c.execute("SELECT user_id, last_seen FROM user_presence")
        presence_rows = c.fetchall()
        active_users_now = 0
        for row in presence_rows:
            row = _row_to_dict(row)
            last_seen = row.get('last_seen') if hasattr(row, 'keys') else row[1]
            dt = _parse_dt(last_seen)
            if dt and dt >= active_cutoff:
                active_users_now += 1

        if active_users_now == 0:
            try:
                if db_type == 'postgres':
                    c.execute("SELECT COUNT(DISTINCT user_id) FROM daily_actions WHERE timestamp >= %s", (active_cutoff.isoformat(),))
                else:
                    c.execute("SELECT COUNT(DISTINCT user_id) FROM daily_actions WHERE timestamp >= ?", (active_cutoff.isoformat(),))
                active_row = c.fetchone()
                active_users_now = int(_row_first_value(active_row, 0) or 0)
            except Exception:
                pass

        # Recent signups.
        c.execute("SELECT id, name, email, role, created_at FROM users ORDER BY created_at DESC LIMIT 8")
        signup_rows = c.fetchall()
        recent_signups = []
        for row in signup_rows:
            row = _row_to_dict(row)
            if hasattr(row, 'keys'):
                recent_signups.append({
                    "id": row.get('id'),
                    "name": row.get('name') or "Unknown",
                    "email": row.get('email') or "",
                    "role": row.get('role') or "user",
                    "created_at": row.get('created_at')
                })
            else:
                recent_signups.append({
                    "id": row[0],
                    "name": row[1] or "Unknown",
                    "email": row[2] or "",
                    "role": row[3] or "user",
                    "created_at": row[4]
                })

        # Total users.
        c.execute("SELECT COUNT(*) FROM users")
        total_users_row = c.fetchone()
        total_users = int(_row_first_value(total_users_row, 0) or 0)

        # Daily active users (from daily_actions table).
        c.execute("""
            SELECT event_date, COUNT(DISTINCT user_id) AS cnt
            FROM daily_actions
            WHERE user_id IS NOT NULL
            GROUP BY event_date
            ORDER BY event_date DESC
            LIMIT 14
        """)
        dau_rows = c.fetchall()
        dau_series = []
        for row in reversed(dau_rows):
            row = _row_to_dict(row)
            if hasattr(row, 'keys'):
                dau_series.append({"date": row.get('event_date'), "count": int(row.get('cnt') or 0)})
            else:
                dau_series.append({"date": row[0], "count": int(row[1] or 0)})

        # User growth (new users/day).
        c.execute("SELECT created_at FROM users")
        created_rows = c.fetchall()
        growth_counter = defaultdict(int)
        for row in created_rows:
            row = _row_to_dict(row)
            val = row.get('created_at') if hasattr(row, 'keys') else row[0]
            dt = _parse_dt(val)
            if dt:
                growth_counter[dt.date().isoformat()] += 1
        growth_dates = sorted(growth_counter.keys())[-14:]
        growth_series = [{"date": d, "count": growth_counter[d]} for d in growth_dates]

        # Retention windows from daily_actions activity.
        c.execute("SELECT id, created_at FROM users")
        all_user_rows = c.fetchall()
        c.execute("SELECT user_id, timestamp FROM daily_actions WHERE user_id IS NOT NULL")
        action_rows = c.fetchall()
        active_1d = set()
        active_7d = set()
        active_30d = set()
        for row in action_rows:
            row = _row_to_dict(row)
            uid = row.get('user_id') if hasattr(row, 'keys') else row[0]
            ts = row.get('timestamp') if hasattr(row, 'keys') else row[1]
            dt = _parse_dt(ts)
            if not dt or uid is None:
                continue
            if dt >= retention_1d_cutoff:
                active_1d.add(uid)
            if dt >= retention_7d_cutoff:
                active_7d.add(uid)
            if dt >= retention_30d_cutoff:
                active_30d.add(uid)

        base_users_for_1d = 0
        base_users_for_7d = 0
        base_users_for_30d = 0
        for row in all_user_rows:
            row = _row_to_dict(row)
            created_at = row.get('created_at') if hasattr(row, 'keys') else row[1]
            dt = _parse_dt(created_at)
            if dt and dt <= retention_1d_cutoff:
                base_users_for_1d += 1
            if dt and dt <= retention_7d_cutoff:
                base_users_for_7d += 1
            if dt and dt <= retention_30d_cutoff:
                base_users_for_30d += 1

        retention = {
            "day_1": round((len(active_1d) / base_users_for_1d) * 100, 2) if base_users_for_1d else 0,
            "day_7": round((len(active_7d) / base_users_for_7d) * 100, 2) if base_users_for_7d else 0,
            "day_30": round((len(active_30d) / base_users_for_30d) * 100, 2) if base_users_for_30d else 0
        }

        # Conversion rate: users with at least one like/save.
        c.execute("""
            SELECT COUNT(DISTINCT user_id) FROM (
                SELECT user_id FROM likes
                UNION
                SELECT user_id FROM saves
            ) t
        """)
        conv_row = c.fetchone()
        converted_users = int(_row_first_value(conv_row, 0) or 0)
        conversion_rate = round((converted_users / total_users) * 100, 2) if total_users else 0

        # Top active users by actions.
        c.execute("""
            SELECT da.user_id, COUNT(*) AS cnt, u.name, u.email
            FROM daily_actions da
            LEFT JOIN users u ON u.id = da.user_id
            WHERE da.user_id IS NOT NULL
            GROUP BY da.user_id, u.name, u.email
            ORDER BY cnt DESC
            LIMIT 8
        """)
        top_rows = c.fetchall()
        top_active_users = []
        for row in top_rows:
            row = _row_to_dict(row)
            if hasattr(row, 'keys'):
                top_active_users.append({
                    "user_id": row.get('user_id'),
                    "name": row.get('name') or "Unknown",
                    "email": row.get('email') or "",
                    "actions": int(row.get('cnt') or 0)
                })
            else:
                top_active_users.append({
                    "user_id": row[0],
                    "name": row[2] or "Unknown",
                    "email": row[3] or "",
                    "actions": int(row[1] or 0)
                })

        # Most used features.
        c.execute("""
            SELECT action, COUNT(*) AS cnt
            FROM daily_actions
            WHERE action IS NOT NULL
            GROUP BY action
            ORDER BY cnt DESC
            LIMIT 10
        """)
        feature_rows = c.fetchall()
        most_used_features = []
        for row in feature_rows:
            row = _row_to_dict(row)
            if hasattr(row, 'keys'):
                most_used_features.append({"feature": row.get('action') or "unknown", "count": int(row.get('cnt') or 0)})
            else:
                most_used_features.append({"feature": row[0] or "unknown", "count": int(row[1] or 0)})

        # Revenue (if donation events are being inserted externally).
        c.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM donation_events WHERE status = 'paid'")
        rev_row = c.fetchone()
        revenue_cents = int(_row_first_value(rev_row, 0) or 0)

        # Scheduled/sent announcement counts.
        c.execute("SELECT COUNT(*) FROM admin_announcements WHERE status = 'scheduled'")
        scheduled_row = c.fetchone()
        announcements_scheduled = int(_row_first_value(scheduled_row, 0) or 0)
        c.execute("SELECT COUNT(*) FROM admin_announcements WHERE status = 'sent'")
        sent_row = c.fetchone()
        announcements_sent = int(_row_first_value(sent_row, 0) or 0)

        # Maintenance mode state
        if db_type == 'postgres':
            c.execute("SELECT value FROM system_settings WHERE key = %s", ('maintenance_mode',))
        else:
            c.execute("SELECT value FROM system_settings WHERE key = ?", ('maintenance_mode',))
        maint_row = c.fetchone()
        maintenance_mode = False
        if maint_row:
            maint_row = _row_to_dict(maint_row)
            maint_val = maint_row.get('value') if hasattr(maint_row, 'keys') else maint_row[0]
            maintenance_mode = str(maint_val).strip().lower() in ('1', 'true', 'yes', 'on')

        conn.commit()
        conn.close()
        return jsonify({
            "active_users_now": active_users_now,
            "recent_signups": recent_signups,
            "daily_active_users": dau_series,
            "user_growth": growth_series,
            "retention": retention,
            "conversion_rate": conversion_rate,
            "top_active_users": top_active_users,
            "most_used_features": most_used_features,
            "revenue_cents": revenue_cents,
            "announcements_scheduled": announcements_scheduled,
            "announcements_sent": announcements_sent,
            "total_users": total_users,
            "maintenance_mode": maintenance_mode
        })
    except Exception as e:
        print(f"[ERROR] Admin insights: {e}")
        if conn:
            try:
                conn.rollback()
                conn.close()
            except:
                pass
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/announcements', methods=['GET'])
@admin_required
@require_permission('view_audit')
def list_announcements():
    conn = None
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        _ensure_admin_feature_tables(conn, c, db_type)
        c.execute("""
            SELECT id, title, message, is_global, target_user_id, scheduled_for, status, created_by, created_at, sent_at
            FROM admin_announcements
            ORDER BY created_at DESC
            LIMIT 100
        """)
        rows = c.fetchall()
        conn.close()
        out = []
        for row in rows:
            row = _row_to_dict(row)
            if hasattr(row, 'keys'):
                out.append({k: row.get(k) for k in ['id', 'title', 'message', 'is_global', 'target_user_id', 'scheduled_for', 'status', 'created_by', 'created_at', 'sent_at']})
            else:
                out.append({
                    "id": row[0], "title": row[1], "message": row[2], "is_global": row[3], "target_user_id": row[4],
                    "scheduled_for": row[5], "status": row[6], "created_by": row[7], "created_at": row[8], "sent_at": row[9]
                })
        return jsonify(out)
    except Exception as e:
        print(f"[ERROR] List announcements: {e}")
        if conn:
            try:
                conn.close()
            except:
                pass
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/announcements/<int:announcement_id>', methods=['DELETE'])
@admin_required
@require_permission('view_audit')
def delete_announcement(announcement_id):
    conn = None
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        _ensure_admin_feature_tables(conn, c, db_type)
        if db_type == 'postgres':
            c.execute("DELETE FROM admin_announcements WHERE id = %s", (announcement_id,))
        else:
            c.execute("DELETE FROM admin_announcements WHERE id = ?", (announcement_id,))
        conn.commit()
        conn.close()
        log_action("DELETE_ANNOUNCEMENT", f"Deleted announcement #{announcement_id}", status="success")
        return jsonify({"success": True})
    except Exception as e:
        print(f"[ERROR] Delete announcement: {e}")
        if conn:
            try:
                conn.rollback()
                conn.close()
            except:
                pass
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/notifications', methods=['GET'])
@admin_required
@require_permission('view_audit')
def list_notifications():
    conn = None
    try:
        notif_type = (request.args.get('type') or 'all').strip().lower()
        conn, db_type = get_db()
        c = conn.cursor()
        _ensure_admin_feature_tables(conn, c, db_type)
        if notif_type != 'all':
            if db_type == 'postgres':
                c.execute("""
                    SELECT id, user_id, title, message, notif_type, source, is_read, created_at, sent_at
                    FROM user_notifications
                    WHERE LOWER(notif_type) = %s
                    ORDER BY created_at DESC
                    LIMIT 200
                """, (notif_type,))
            else:
                c.execute("""
                    SELECT id, user_id, title, message, notif_type, source, is_read, created_at, sent_at
                    FROM user_notifications
                    WHERE LOWER(notif_type) = ?
                    ORDER BY created_at DESC
                    LIMIT 200
                """, (notif_type,))
        else:
            c.execute("""
                SELECT id, user_id, title, message, notif_type, source, is_read, created_at, sent_at
                FROM user_notifications
                ORDER BY created_at DESC
                LIMIT 200
            """)
        rows = c.fetchall()
        conn.close()
        out = []
        for row in rows:
            row = _row_to_dict(row)
            if hasattr(row, 'keys'):
                out.append({
                    "id": row.get('id'),
                    "user_id": row.get('user_id'),
                    "title": row.get('title') or '',
                    "message": row.get('message') or '',
                    "notif_type": row.get('notif_type') or '',
                    "source": row.get('source') or '',
                    "is_read": bool(row.get('is_read') or 0),
                    "created_at": row.get('created_at'),
                    "sent_at": row.get('sent_at')
                })
            else:
                out.append({
                    "id": row[0],
                    "user_id": row[1],
                    "title": row[2] or '',
                    "message": row[3] or '',
                    "notif_type": row[4] or '',
                    "source": row[5] or '',
                    "is_read": bool(row[6] or 0),
                    "created_at": row[7],
                    "sent_at": row[8] if len(row) > 8 else None
                })
        return jsonify(out)
    except Exception as e:
        print(f"[ERROR] List notifications: {e}")
        if conn:
            try:
                conn.close()
            except:
                pass
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/notifications/<int:notification_id>', methods=['DELETE'])
@admin_required
@require_permission('view_audit')
def delete_notification(notification_id):
    conn = None
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        _ensure_admin_feature_tables(conn, c, db_type)
        if db_type == 'postgres':
            c.execute("DELETE FROM user_notifications WHERE id = %s", (notification_id,))
        else:
            c.execute("DELETE FROM user_notifications WHERE id = ?", (notification_id,))
        conn.commit()
        conn.close()
        log_action("DELETE_NOTIFICATION", f"Deleted notification #{notification_id}", status="success")
        return jsonify({"success": True})
    except Exception as e:
        print(f"[ERROR] Delete notification: {e}")
        if conn:
            try:
                conn.rollback()
                conn.close()
            except:
                pass
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/announcements', methods=['POST'])
@admin_required
@require_permission('view_audit')
def create_announcement():
    conn = None
    try:
        data = request.get_json() or {}
        title = str(data.get('title') or '').strip() or 'Announcement'
        message = str(data.get('message') or '').strip()
        if not message:
            return jsonify({"error": "Message required"}), 400
        is_global = 1 if bool(data.get('is_global', True)) else 0
        target_user_id = data.get('target_user_id')
        if target_user_id in ('', None):
            target_user_id = None
        else:
            target_user_id = int(target_user_id)
            is_global = 0
        scheduled_for = str(data.get('scheduled_for') or '').strip() or None
        status = 'scheduled' if scheduled_for else 'draft'

        conn, db_type = get_db()
        c = conn.cursor()
        _ensure_admin_feature_tables(conn, c, db_type)
        admin = get_admin_session()
        admin_role = admin['role'] if admin else 'unknown'
        now_iso = _iso_now()

        if db_type == 'postgres':
            c.execute("""
                INSERT INTO admin_announcements (title, message, is_global, target_user_id, scheduled_for, status, created_by, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (title, message, is_global, target_user_id, scheduled_for, status, admin_role, now_iso))
            row = _row_to_dict(c.fetchone())
            announcement_id = row.get('id') if hasattr(row, 'keys') else row[0]
        else:
            c.execute("""
                INSERT INTO admin_announcements (title, message, is_global, target_user_id, scheduled_for, status, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (title, message, is_global, target_user_id, scheduled_for, status, admin_role, now_iso))
            announcement_id = c.lastrowid

        dispatched = 0
        if status == 'draft':
            if db_type == 'postgres':
                c.execute("""
                    SELECT id, title, message, is_global, target_user_id
                    FROM admin_announcements
                    WHERE id = %s
                """, (announcement_id,))
            else:
                c.execute("""
                    SELECT id, title, message, is_global, target_user_id
                    FROM admin_announcements
                    WHERE id = ?
                """, (announcement_id,))
            ann_row = _row_to_dict(c.fetchone())
            dispatched = _dispatch_announcement_row(c, db_type, ann_row)

        log_action(
            "CREATE_ANNOUNCEMENT",
            f"Created announcement #{announcement_id}",
            status="success",
            extras={"is_global": bool(is_global), "target_user_id": target_user_id, "scheduled_for": scheduled_for, "dispatched": dispatched}
        )

        conn.commit()
        conn.close()
        return jsonify({"success": True, "id": announcement_id, "status": ("sent" if status == 'draft' else status), "dispatched": dispatched})
    except Exception as e:
        print(f"[ERROR] Create announcement: {e}")
        if conn:
            try:
                conn.rollback()
                conn.close()
            except:
                pass
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/announcements/<int:announcement_id>/send', methods=['POST'])
@admin_required
@require_permission('view_audit')
def send_announcement_now(announcement_id):
    conn = None
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        _ensure_admin_feature_tables(conn, c, db_type)
        if db_type == 'postgres':
            c.execute("""
                SELECT id, title, message, is_global, target_user_id
                FROM admin_announcements
                WHERE id = %s
            """, (announcement_id,))
        else:
            c.execute("""
                SELECT id, title, message, is_global, target_user_id
                FROM admin_announcements
                WHERE id = ?
            """, (announcement_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Announcement not found"}), 404
        dispatched = _dispatch_announcement_row(c, db_type, row)
        log_action("SEND_ANNOUNCEMENT", f"Sent announcement #{announcement_id}", status="success", extras={"dispatched": dispatched})
        conn.commit()
        conn.close()
        return jsonify({"success": True, "dispatched": dispatched})
    except Exception as e:
        print(f"[ERROR] Send announcement: {e}")
        if conn:
            try:
                conn.rollback()
                conn.close()
            except:
                pass
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/messages/user', methods=['POST'])
@admin_required
@require_permission('view_audit')
def admin_message_user():
    data = request.get_json() or {}
    title = str(data.get('title') or 'Message from Admin').strip()
    message = str(data.get('message') or '').strip()
    target_user_id = data.get('target_user_id')
    if not message:
        return jsonify({"error": "Message required"}), 400
    if target_user_id in (None, ''):
        return jsonify({"error": "target_user_id required"}), 400
    try:
        target_user_id = int(target_user_id)
    except Exception:
        return jsonify({"error": "target_user_id must be numeric"}), 400

    conn = None
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        _ensure_admin_feature_tables(conn, c, db_type)
        now_iso = _iso_now()
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO user_notifications (user_id, title, message, notif_type, source, is_read, created_at, sent_at)
                VALUES (%s, %s, %s, %s, %s, 0, %s, %s)
            """, (target_user_id, title, message, 'direct_message', 'admin', now_iso, now_iso))
        else:
            c.execute("""
                INSERT INTO user_notifications (user_id, title, message, notif_type, source, is_read, created_at, sent_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """, (target_user_id, title, message, 'direct_message', 'admin', now_iso, now_iso))
        log_action("DIRECT_MESSAGE", f"Sent direct message to user {target_user_id}", target_user_id=target_user_id, status="success")
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        print(f"[ERROR] Direct message: {e}")
        if conn:
            try:
                conn.rollback()
                conn.close()
            except:
                pass
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/push/send', methods=['POST'])
@admin_required
@require_permission('view_audit')
def send_push_notification():
    data = request.get_json() or {}
    title = str(data.get('title') or 'Notification').strip()
    message = str(data.get('message') or '').strip()
    if not message:
        return jsonify({"error": "Message required"}), 400
    conn = None
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        _ensure_admin_feature_tables(conn, c, db_type)
        now_iso = _iso_now()
        c.execute("SELECT id FROM users")
        users = c.fetchall()
        sent = 0
        for row in users:
            row = _row_to_dict(row)
            uid = row.get('id') if hasattr(row, 'keys') else row[0]
            if db_type == 'postgres':
                c.execute("""
                    INSERT INTO user_notifications (user_id, title, message, notif_type, source, is_read, created_at, sent_at)
                    VALUES (%s, %s, %s, %s, %s, 0, %s, %s)
                """, (uid, title, message, 'push', 'admin', now_iso, now_iso))
            else:
                c.execute("""
                    INSERT INTO user_notifications (user_id, title, message, notif_type, source, is_read, created_at, sent_at)
                    VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """, (uid, title, message, 'push', 'admin', now_iso, now_iso))
            sent += 1
        log_action("PUSH_BROADCAST", "Broadcast push notification", status="success", extras={"sent_count": sent})
        conn.commit()
        conn.close()
        return jsonify({"success": True, "sent": sent})
    except Exception as e:
        print(f"[ERROR] Push broadcast: {e}")
        if conn:
            try:
                conn.rollback()
                conn.close()
            except:
                pass
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/admin-chat', methods=['GET', 'POST'])
@admin_required
@require_permission('view_audit')
def admin_chat():
    conn = None
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        _ensure_admin_feature_tables(conn, c, db_type)
        if request.method == 'POST':
            data = request.get_json() or {}
            msg = str(data.get('message') or '').strip()
            if not msg:
                conn.close()
                return jsonify({"error": "Message required"}), 400
            admin = get_admin_session()
            role = admin['role'] if admin else 'unknown'
            now_iso = _iso_now()
            if db_type == 'postgres':
                c.execute("INSERT INTO admin_chat_messages (admin_role, message, created_at) VALUES (%s, %s, %s)", (role, msg, now_iso))
            else:
                c.execute("INSERT INTO admin_chat_messages (admin_role, message, created_at) VALUES (?, ?, ?)", (role, msg, now_iso))
            conn.commit()
            log_action("ADMIN_CHAT_MESSAGE", "Posted admin chat message", status="success")
            conn.close()
            return jsonify({"success": True})

        c.execute("""
            SELECT id, admin_role, message, created_at
            FROM admin_chat_messages
            ORDER BY created_at DESC
            LIMIT 100
        """)
        rows = c.fetchall()
        conn.close()
        out = []
        for row in reversed(rows):
            row = _row_to_dict(row)
            if hasattr(row, 'keys'):
                out.append({
                    "id": row.get('id'),
                    "admin_role": row.get('admin_role') or "unknown",
                    "message": row.get('message') or "",
                    "created_at": row.get('created_at')
                })
            else:
                out.append({
                    "id": row[0],
                    "admin_role": row[1] or "unknown",
                    "message": row[2] or "",
                    "created_at": row[3]
                })
        return jsonify(out)
    except Exception as e:
        print(f"[ERROR] Admin chat: {e}")
        if conn:
            try:
                conn.close()
            except:
                pass
        return jsonify({"error": str(e)}), 500

# System Settings API
@admin_bp.route('/api/system/settings', methods=['GET'])
@admin_required
def get_system_settings():
    """Get system settings including verse refresh interval"""
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        
        # Ensure settings table exists
        c.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        defaults = {
            "verse_interval": "60",
            "auto_refresh_seconds": "30",
            "audit_retention_days": "90",
            "safety_mode": "balanced",
            "show_user_persona": "1",
            "maintenance_mode": "0"
        }

        placeholders = ",".join(["%s"] * len(defaults)) if db_type == 'postgres' else ",".join(["?"] * len(defaults))
        c.execute(
            f"SELECT key, value FROM system_settings WHERE key IN ({placeholders})",
            tuple(defaults.keys())
        )
        rows = c.fetchall()
        stored = {}
        for row in rows:
            row = _row_to_dict(row)
            if hasattr(row, 'keys'):
                stored[str(row.get('key'))] = str(row.get('value'))
            else:
                stored[str(row[0])] = str(row[1])
        conn.close()

        merged = {**defaults, **stored}
        return jsonify({
            "verse_interval": int(merged["verse_interval"]),
            "auto_refresh_seconds": int(merged["auto_refresh_seconds"]),
            "audit_retention_days": int(merged["audit_retention_days"]),
            "safety_mode": merged["safety_mode"],
            "show_user_persona": str(merged["show_user_persona"]).lower() in ("1", "true", "yes", "on"),
            "maintenance_mode": str(merged["maintenance_mode"]).lower() in ("1", "true", "yes", "on"),
            "success": True
        })
    except Exception as e:
        print(f"[ERROR] Get system settings: {e}")
        return jsonify({
            "verse_interval": 60,
            "auto_refresh_seconds": 30,
            "audit_retention_days": 90,
            "safety_mode": "balanced",
            "show_user_persona": True,
            "maintenance_mode": False,
            "success": True
        })

@admin_bp.route('/api/system/settings', methods=['PUT'])
@admin_required
@require_permission('edit_settings')
def update_system_settings():
    """Update system settings"""
    data = request.get_json() or {}
    
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        
        # Ensure settings table exists
        c.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        updates = []

        # Update verse_interval if provided
        if 'verse_interval' in data:
            interval = int(data['verse_interval'])
            # Validate interval (must be between 10 and 3600 seconds)
            if interval < 10 or interval > 3600:
                conn.close()
                return jsonify({"error": "Interval must be between 10 and 3600 seconds"}), 400

            if db_type == 'postgres':
                c.execute("""
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES ('verse_interval', %s, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = EXCLUDED.value,
                        updated_at = EXCLUDED.updated_at
                """, (str(interval),))
            else:
                c.execute("""
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES ('verse_interval', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                """, (str(interval),))
            updates.append(f"verse_interval={interval}")
            
            # Update the running generator's interval
            try:
                from app import generator
                generator.set_interval(interval)
                print(f"[INFO] Updated generator interval to {interval} seconds")
            except Exception as gen_err:
                print(f"[WARN] Could not update generator interval: {gen_err}")

        if 'auto_refresh_seconds' in data:
            auto_refresh = int(data['auto_refresh_seconds'])
            if auto_refresh < 10 or auto_refresh > 300:
                conn.close()
                return jsonify({"error": "Auto refresh must be between 10 and 300 seconds"}), 400
            if db_type == 'postgres':
                c.execute("""
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES ('auto_refresh_seconds', %s, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
                """, (str(auto_refresh),))
            else:
                c.execute("""
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES ('auto_refresh_seconds', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """, (str(auto_refresh),))
            updates.append(f"auto_refresh_seconds={auto_refresh}")

        if 'audit_retention_days' in data:
            retention_days = int(data['audit_retention_days'])
            if retention_days < 7 or retention_days > 365:
                conn.close()
                return jsonify({"error": "Audit retention must be between 7 and 365 days"}), 400
            if db_type == 'postgres':
                c.execute("""
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES ('audit_retention_days', %s, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
                """, (str(retention_days),))
            else:
                c.execute("""
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES ('audit_retention_days', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """, (str(retention_days),))
            updates.append(f"audit_retention_days={retention_days}")

        if 'safety_mode' in data:
            safety_mode = str(data['safety_mode']).strip().lower()
            if safety_mode not in ('strict', 'balanced', 'relaxed'):
                conn.close()
                return jsonify({"error": "Safety mode must be strict, balanced, or relaxed"}), 400
            if db_type == 'postgres':
                c.execute("""
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES ('safety_mode', %s, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
                """, (safety_mode,))
            else:
                c.execute("""
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES ('safety_mode', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """, (safety_mode,))
            updates.append(f"safety_mode={safety_mode}")

        if 'show_user_persona' in data:
            raw_persona = data['show_user_persona']
            if isinstance(raw_persona, str):
                show_user_persona = 1 if raw_persona.strip().lower() in ('1', 'true', 'yes', 'on') else 0
            else:
                show_user_persona = 1 if bool(raw_persona) else 0
            if db_type == 'postgres':
                c.execute("""
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES ('show_user_persona', %s, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
                """, (str(show_user_persona),))
            else:
                c.execute("""
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES ('show_user_persona', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """, (str(show_user_persona),))
            updates.append(f"show_user_persona={show_user_persona}")

        if 'maintenance_mode' in data:
            raw_maintenance = data['maintenance_mode']
            if isinstance(raw_maintenance, str):
                maintenance_mode = 1 if raw_maintenance.strip().lower() in ('1', 'true', 'yes', 'on') else 0
            else:
                maintenance_mode = 1 if bool(raw_maintenance) else 0
            if db_type == 'postgres':
                c.execute("""
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES ('maintenance_mode', %s, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
                """, (str(maintenance_mode),))
            else:
                c.execute("""
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES ('maintenance_mode', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """, (str(maintenance_mode),))
            updates.append(f"maintenance_mode={maintenance_mode}")

        if updates:
            log_action(
                "UPDATE_SETTINGS",
                "Updated system settings: " + ", ".join(updates),
                status="success",
                extras={"updated_keys": updates, "module": "settings"}
            )

        conn.commit()
        conn.close()
        
        return jsonify({"success": True})
    except Exception as e:
        print(f"[ERROR] Update system settings: {e}")
        return jsonify({"error": str(e)}), 500


# ========== ADMIN XP MANAGEMENT ==========

@admin_bp.route('/api/users/<int:user_id>/give-xp', methods=['POST'])
@admin_required
@require_permission('manage_xp')
def admin_give_xp(user_id):
    """Admin endpoint to give XP to a user"""
    admin = get_admin_session()
    data = request.get_json()
    amount = data.get('amount', 0)
    reason = data.get('reason', 'Admin XP gift')
    send_notification = data.get('notify', True)
    
    if amount <= 0:
        return jsonify({"error": "Amount must be positive"}), 400
    
    conn, db_type = get_db()
    c = conn.cursor()
    
    try:
        # Get user info
        if db_type == 'postgres':
            c.execute("SELECT name, email FROM users WHERE id = %s", (user_id,))
        else:
            c.execute("SELECT name, email FROM users WHERE id = ?", (user_id,))
        
        user = c.fetchone()
        if not user:
            conn.close()
            return jsonify({"error": "User not found"}), 404
        
        user_name = user[0] if user else "Unknown"
        
        # Get or create user XP record
        if db_type == 'postgres':
            c.execute("SELECT xp, total_xp_earned, level FROM user_xp WHERE user_id = %s", (user_id,))
        else:
            c.execute("SELECT xp, total_xp_earned, level FROM user_xp WHERE user_id = ?", (user_id,))
        
        row = c.fetchone()
        
        if row:
            current_xp = row[0] or 0
            total_earned = row[1] or 0
            current_level = row[2] or 1
        else:
            current_xp = 0
            total_earned = 0
            current_level = 1
        
        # Calculate new values
        new_xp = current_xp + amount
        new_total = total_earned + amount
        new_level = (new_total // 1000) + 1
        leveled_up = new_level > current_level
        
        # Update user XP
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO user_xp (user_id, xp, total_xp_earned, level, updated_at)
                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO UPDATE SET
                    xp = EXCLUDED.xp,
                    total_xp_earned = EXCLUDED.total_xp_earned,
                    level = EXCLUDED.level,
                    updated_at = CURRENT_TIMESTAMP
            """, (user_id, new_xp, new_total, new_level))
            
            # Log transaction
            c.execute("""
                INSERT INTO xp_transactions (user_id, amount, type, description, timestamp)
                VALUES (%s, %s, 'admin_gift', %s, CURRENT_TIMESTAMP)
            """, (user_id, amount, f"Admin gift from {admin['role']}: {reason}"))
        else:
            c.execute("""
                INSERT OR REPLACE INTO user_xp (user_id, xp, total_xp_earned, level, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
            """, (user_id, new_xp, new_total, new_level))
            
            c.execute("""
                INSERT INTO xp_transactions (user_id, amount, type, description, timestamp)
                VALUES (?, ?, 'admin_gift', ?, datetime('now'))
            """, (user_id, amount, f"Admin gift from {admin['role']}: {reason}"))
        
        conn.commit()
        
        # Log admin action
        log_action(
            "GIVE_XP",
            f"Gave {amount} XP to {user_name} ({user_id})",
            target_user_id=user_id,
            status="success",
            extras={"amount": amount, "reason": reason, "module": "xp_management"},
            target={"user_id": user_id, "name": user_name}
        )
        
        conn.close()
        
        return jsonify({
            "success": True,
            "user_id": user_id,
            "user_name": user_name,
            "amount": amount,
            "new_total": new_xp,
            "new_level": new_level,
            "leveled_up": leveled_up,
            "message": f"Successfully gave {amount} XP to {user_name}"
        })
    except Exception as e:
        print(f"[ERROR] Admin give XP: {e}")
        import traceback
        traceback.print_exc()
        conn.close()
        return jsonify({"error": str(e)}), 500


@admin_bp.route('/api/users/<int:user_id>/xp-history')
@admin_required
def get_user_xp_history(user_id):
    """Get XP transaction history for a user"""
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        
        if db_type == 'postgres':
            c.execute("""
                SELECT amount, type, description, timestamp
                FROM xp_transactions
                WHERE user_id = %s
                ORDER BY timestamp DESC
                LIMIT 50
            """, (user_id,))
        else:
            c.execute("""
                SELECT amount, type, description, timestamp
                FROM xp_transactions
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT 50
            """, (user_id,))
        
        transactions = []
        for row in c.fetchall():
            transactions.append({
                "amount": row[0],
                "type": row[1],
                "description": row[2],
                "timestamp": row[3]
            })
        
        # Get current XP
        if db_type == 'postgres':
            c.execute("SELECT xp, level FROM user_xp WHERE user_id = %s", (user_id,))
        else:
            c.execute("SELECT xp, level FROM user_xp WHERE user_id = ?", (user_id,))
        
        xp_row = c.fetchone()
        current_xp = xp_row[0] if xp_row else 0
        level = xp_row[1] if xp_row else 1
        
        conn.close()
        
        return jsonify({
            "user_id": user_id,
            "current_xp": current_xp,
            "level": level,
            "transactions": transactions
        })
    except Exception as e:
        print(f"[ERROR] Get XP history: {e}")
        return jsonify({"error": str(e)}), 500


@admin_bp.route('/api/users/<int:user_id>/stats')
@admin_required
def get_user_stats(user_id):
    """Get comprehensive stats for a user including XP"""
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        
        # Get user info
        if db_type == 'postgres':
            c.execute("SELECT name, email, role FROM users WHERE id = %s", (user_id,))
        else:
            c.execute("SELECT name, email, role FROM users WHERE id = ?", (user_id,))
        
        user = c.fetchone()
        if not user:
            conn.close()
            return jsonify({"error": "User not found"}), 404
        
        user_name, user_email, user_role = user
        
        # Get stats
        if db_type == 'postgres':
            c.execute("SELECT COUNT(*) FROM likes WHERE user_id = %s", (user_id,))
            likes = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM saves WHERE user_id = %s", (user_id,))
            saves = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM comments WHERE user_id = %s AND is_deleted = 0", (user_id,))
            comments = c.fetchone()[0]
            
            c.execute("SELECT xp, level FROM user_xp WHERE user_id = %s", (user_id,))
        else:
            c.execute("SELECT COUNT(*) FROM likes WHERE user_id = ?", (user_id,))
            likes = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM saves WHERE user_id = ?", (user_id,))
            saves = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM comments WHERE user_id = ? AND is_deleted = 0", (user_id,))
            comments = c.fetchone()[0]
            
            c.execute("SELECT xp, level FROM user_xp WHERE user_id = ?", (user_id,))
        
        xp_row = c.fetchone()
        xp = xp_row[0] if xp_row else 0
        level = xp_row[1] if xp_row else 1
        
        conn.close()
        
        return jsonify({
            "user_id": user_id,
            "name": user_name,
            "email": user_email,
            "role": user_role,
            "likes": likes,
            "saves": saves,
            "comments": comments,
            "xp": xp,
            "level": level
        })
    except Exception as e:
        print(f"[ERROR] Get user stats: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
