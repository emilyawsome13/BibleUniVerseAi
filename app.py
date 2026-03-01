from flask import Flask, render_template, jsonify, request, redirect, url_for, session, send_from_directory, flash, render_template_string
import sqlite3
import time
import threading
import requests
import os
import re
import secrets
import json
import random
import logging
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta, timezone
import hashlib
from functools import wraps
from urllib.parse import quote

# Load environment variables from .env file (for local development)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, use system env vars

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.permanent_session_lifetime = timedelta(days=30)

@app.before_request
def make_session_permanent():
    session.permanent = True

# Configuration
app.secret_key = os.environ.get('SECRET_KEY', 'eK8#mP2$vL9@nQ4&wX5*fJ7!hR3(tY6)bU1$cI0~pO8+lA2=zS9')
PUBLIC_URL = os.environ.get('PUBLIC_URL') or os.environ.get('RENDER_EXTERNAL_URL') or 'https://aibible.onrender.com'

GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '420462376171-neu8kbc7cm1geu2ov70gd10fh9e2210i.apps.googleusercontent.com')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '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')
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
OPENAI_API_URL = os.environ.get('OPENAI_API_URL', 'https://api.openai.com/v1/chat/completions')
OPENAI_MODEL = os.environ.get('OPENAI_MODEL', 'gpt-4.1')
BIBLE_API_BASE = "https://bible-api.com"
DEFAULT_TRANSLATION = os.environ.get('BIBLE_API_TRANSLATION', 'web').lower()

MOOD_KEYWORDS = {
    "peace": ["peace", "calm", "rest", "still"],
    "strength": ["strength", "strong", "power", "courage", "mighty"],
    "hope": ["hope", "future", "promise", "trust", "faith"],
    "love": ["love", "beloved", "mercy", "grace", "compassion"],
    "gratitude": ["thanks", "thank", "grateful", "praise", "give thanks"],
    "guidance": ["guide", "path", "direct", "wisdom", "counsel"]
}

# Role-based codes
ROLE_CODES = {
    'user': None,  # No code needed
    'host': os.environ.get('HOST_CODE', 'HOST123'),
    'mod': os.environ.get('MOD_CODE', 'MOD456'),
    'co_owner': os.environ.get('CO_OWNER_CODE', 'COOWNER789'),
    'owner': os.environ.get('OWNER_CODE', 'OWNER999')
}

def normalize_role(role):
    value = str(role or 'user').strip().lower()
    if value in ('co-owner', 'co owner', 'coowner'):
        return 'co_owner'
    if value in ('owner', 'host', 'mod'):
        return value
    return value or 'user'

def role_priority(role):
    order = {'user': 0, 'host': 1, 'mod': 2, 'co_owner': 3, 'owner': 4}
    return order.get(normalize_role(role), 0)

def get_user_equipped_items(c, db_type, user_id):
    """Get a user's equipped profile items for display on comments/messages"""
    if not user_id:
        return {
            "frame": None,
            "name_color": None,
            "title": None,
            "badges": [],
            "chat_effect": None,
            "profile_bg": None
        }
    
    try:
        if db_type == 'postgres':
            c.execute("""
                SELECT s.item_id, s.category, s.effects, s.name, s.icon, s.rarity
                FROM user_inventory i
                JOIN shop_items s ON i.item_id = s.item_id
                WHERE i.user_id = %s AND i.equipped = TRUE
            """, (user_id,))
        else:
            c.execute("""
                SELECT s.item_id, s.category, s.effects, s.name, s.icon, s.rarity
                FROM user_inventory i
                JOIN shop_items s ON i.item_id = s.item_id
                WHERE i.user_id = ? AND i.equipped = 1
            """, (user_id,))
        
        equipped = {
            "frame": None,
            "name_color": None,
            "title": None,
            "badges": [],
            "chat_effect": None,
            "profile_bg": None
        }
        
        for row in c.fetchall():
            try:
                category = row['category'] if hasattr(row, 'keys') else row[1]
                effects = row['effects'] if isinstance(row['effects'], dict) else json.loads(row['effects'] or '{}')
                item_data = {
                    "item_id": row['item_id'] if hasattr(row, 'keys') else row[0],
                    "name": row['name'] if hasattr(row, 'keys') else row[3],
                    "icon": row['icon'] if hasattr(row, 'keys') else row[4],
                    "rarity": row['rarity'] if hasattr(row, 'keys') else row[5],
                    "effects": effects
                }
                
                if category == 'badge':
                    equipped['badges'].append(item_data)
                elif category in equipped:
                    equipped[category] = item_data
            except Exception as e:
                logger.error(f"Error parsing equipped item: {e}")
                continue
        
        return equipped
    except Exception as e:
        logger.error(f"Error getting equipped items: {e}")
        return {
            "frame": None,
            "name_color": None,
            "title": None,
            "badges": [],
            "chat_effect": None,
            "profile_bg": None
        }

ADMIN_CODE = os.environ.get('ADMIN_CODE', 'God Is All')
MASTER_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'God Is All')

ALLOWED_IMAGE_EXTS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
UPLOAD_ROOT = os.path.join(app.root_path, 'static', 'uploads')

def allowed_image_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXTS

def require_min_role(min_role='host'):
    role = normalize_role(session.get('admin_role') or session.get('role') or 'user')
    return role_priority(role) >= role_priority(min_role)

def try_remove_background(file_path):
    try:
        from PIL import Image
    except Exception as e:
        return False, f"Pillow not available: {e}"
    try:
        img = Image.open(file_path).convert("RGBA")
        bg = img.getpixel((0, 0))
        new_data = []
        for r, g, b, a in img.getdata():
            if abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2]) < 45:
                new_data.append((r, g, b, 0))
            else:
                new_data.append((r, g, b, a))
        img.putdata(new_data)
        img.save(file_path)
        return True, None
    except Exception as e:
        return False, str(e)

RAW_DATABASE_URL = (
    os.environ.get('DATABASE_URL')
    or os.environ.get('DATABASE_URL-9864bd776330b2743effe162f4cef50d')
)

def _resolve_database_url(value):
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if '://' in raw:
        return raw
    # Allow indirection: DATABASE_URL=some_key -> use env var with that name or DATABASE_URL-some_key
    if raw in os.environ:
        return os.environ.get(raw)
    alt_key = f"DATABASE_URL-{raw}"
    if alt_key in os.environ:
        return os.environ.get(alt_key)
    return raw

DATABASE_URL = _resolve_database_url(RAW_DATABASE_URL) or 'sqlite:///bible_ios.db'
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

RENDER_ENV = bool(os.environ.get('RENDER') or os.environ.get('RENDER_SERVICE_ID'))
DB_MODE = str(os.environ.get('DB_MODE') or ('auto' if RENDER_ENV else 'sqlite')).strip().lower()
FORCE_SQLITE = DB_MODE in ('sqlite', 'local', 'file')
FORCE_POSTGRES = DB_MODE in ('postgres', 'postgresql', 'pg')
IS_POSTGRES = (not FORCE_SQLITE) and DATABASE_URL and ('postgresql' in DATABASE_URL or 'postgres' in DATABASE_URL)
POSTGRES_AVAILABLE = True
STRICT_DB = str(os.environ.get('STRICT_DB', '1')).strip().lower() in ('1', 'true', 'yes', 'on')
if FORCE_SQLITE:
    DATABASE_URL = 'sqlite:///bible_ios.db'
if RENDER_ENV and FORCE_SQLITE:
    raise RuntimeError("DB_MODE=sqlite is not allowed in production. Set DB_MODE=postgres and DATABASE_URL.")
if RENDER_ENV and not IS_POSTGRES:
    raise RuntimeError("Postgres DATABASE_URL is required in production (Render).")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SQLITE_PATH = os.path.join(BASE_DIR, 'bible_ios.db')
BOOK_TEXT_CACHE = {}
BOOK_META_CACHE = {}
BAN_SCHEMA_READY = False
RESTRICTION_SCHEMA_READY = False
SQLITE_TUNING_APPLIED = False
_SCHEMA_READY_FLAGS = set()
_SCHEMA_READY_LOCK = threading.Lock()
BAN_STATUS_CACHE_TTL = max(1.0, float(os.environ.get('BAN_STATUS_CACHE_TTL', '3.0')))
_BAN_STATUS_CACHE = {}
_BAN_STATUS_CACHE_LOCK = threading.Lock()

FALLBACK_BOOKS = [
    {"id": "GEN", "name": "Genesis"},
    {"id": "EXO", "name": "Exodus"},
    {"id": "LEV", "name": "Leviticus"},
    {"id": "NUM", "name": "Numbers"},
    {"id": "DEU", "name": "Deuteronomy"},
    {"id": "JOS", "name": "Joshua"},
    {"id": "JDG", "name": "Judges"},
    {"id": "RUT", "name": "Ruth"},
    {"id": "1SA", "name": "1 Samuel"},
    {"id": "2SA", "name": "2 Samuel"},
    {"id": "1KI", "name": "1 Kings"},
    {"id": "2KI", "name": "2 Kings"},
    {"id": "1CH", "name": "1 Chronicles"},
    {"id": "2CH", "name": "2 Chronicles"},
    {"id": "EZR", "name": "Ezra"},
    {"id": "NEH", "name": "Nehemiah"},
    {"id": "EST", "name": "Esther"},
    {"id": "JOB", "name": "Job"},
    {"id": "PSA", "name": "Psalms"},
    {"id": "PRO", "name": "Proverbs"},
    {"id": "ECC", "name": "Ecclesiastes"},
    {"id": "SNG", "name": "Song of Solomon"},
    {"id": "ISA", "name": "Isaiah"},
    {"id": "JER", "name": "Jeremiah"},
    {"id": "LAM", "name": "Lamentations"},
    {"id": "EZK", "name": "Ezekiel"},
    {"id": "DAN", "name": "Daniel"},
    {"id": "HOS", "name": "Hosea"},
    {"id": "JOL", "name": "Joel"},
    {"id": "AMO", "name": "Amos"},
    {"id": "OBA", "name": "Obadiah"},
    {"id": "JON", "name": "Jonah"},
    {"id": "MIC", "name": "Micah"},
    {"id": "NAM", "name": "Nahum"},
    {"id": "HAB", "name": "Habakkuk"},
    {"id": "ZEP", "name": "Zephaniah"},
    {"id": "HAG", "name": "Haggai"},
    {"id": "ZEC", "name": "Zechariah"},
    {"id": "MAL", "name": "Malachi"},
    {"id": "MAT", "name": "Matthew"},
    {"id": "MRK", "name": "Mark"},
    {"id": "LUK", "name": "Luke"},
    {"id": "JHN", "name": "John"},
    {"id": "ACT", "name": "Acts"},
    {"id": "ROM", "name": "Romans"},
    {"id": "1CO", "name": "1 Corinthians"},
    {"id": "2CO", "name": "2 Corinthians"},
    {"id": "GAL", "name": "Galatians"},
    {"id": "EPH", "name": "Ephesians"},
    {"id": "PHP", "name": "Philippians"},
    {"id": "COL", "name": "Colossians"},
    {"id": "1TH", "name": "1 Thessalonians"},
    {"id": "2TH", "name": "2 Thessalonians"},
    {"id": "1TI", "name": "1 Timothy"},
    {"id": "2TI", "name": "2 Timothy"},
    {"id": "TIT", "name": "Titus"},
    {"id": "PHM", "name": "Philemon"},
    {"id": "HEB", "name": "Hebrews"},
    {"id": "JAS", "name": "James"},
    {"id": "1PE", "name": "1 Peter"},
    {"id": "2PE", "name": "2 Peter"},
    {"id": "1JN", "name": "1 John"},
    {"id": "2JN", "name": "2 John"},
    {"id": "3JN", "name": "3 John"},
    {"id": "JUD", "name": "Jude"},
    {"id": "REV", "name": "Revelation"},
]

_BIBLE_BOOK_ALIASES = {
    "psalm": "psalms",
    "songofsongs": "songofsolomon",
    "canticles": "songofsolomon",
}

def _normalize_bible_book_name(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"\bfirst\b|\b1st\b", "1", text)
    text = re.sub(r"\bsecond\b|\b2nd\b", "2", text)
    text = re.sub(r"\bthird\b|\b3rd\b", "3", text)
    text = re.sub(r"\bi\b", "1", text)
    text = re.sub(r"\bii\b", "2", text)
    text = re.sub(r"\biii\b", "3", text)
    return re.sub(r"[^a-z0-9]", "", text)

def _extract_book_from_reference(reference):
    ref = str(reference or "").strip()
    if not ref:
        return ""
    match = re.match(r"^\s*([1-3]?\s*[A-Za-z][A-Za-z\s]+?)\s+\d+", ref)
    if match:
        return match.group(1).strip()
    return ""

def _reference_sort_parts(reference):
    ref = str(reference or "").strip()
    if not ref:
        return (10**9, 10**9)
    match = re.match(r"^\s*[1-3]?\s*[A-Za-z][A-Za-z\s]+\s+(\d+)(?:\s*:\s*(\d+))?", ref)
    if match:
        chapter = int(match.group(1))
        verse = int(match.group(2)) if match.group(2) else 0
        return (chapter, verse)
    match = re.search(r"(\d+)\s*:\s*(\d+)", ref)
    if match:
        return (int(match.group(1)), int(match.group(2)))
    return (10**9, 10**9)

BIBLE_BOOK_ORDER = {}
for index, entry in enumerate(FALLBACK_BOOKS):
    key = _normalize_bible_book_name(entry.get("name"))
    if key and key not in BIBLE_BOOK_ORDER:
        BIBLE_BOOK_ORDER[key] = index

for alias_name, target_name in _BIBLE_BOOK_ALIASES.items():
    target_key = _normalize_bible_book_name(target_name)
    alias_key = _normalize_bible_book_name(alias_name)
    if target_key in BIBLE_BOOK_ORDER and alias_key:
        BIBLE_BOOK_ORDER[alias_key] = BIBLE_BOOK_ORDER[target_key]

def _verse_matches_title_query(verse, query_key):
    if not query_key:
        return True
    book_name = verse.get("book") or _extract_book_from_reference(verse.get("ref"))
    book_key = _normalize_bible_book_name(book_name)
    ref_key = _normalize_bible_book_name(verse.get("ref"))
    if book_key in _BIBLE_BOOK_ALIASES:
        book_key = _normalize_bible_book_name(_BIBLE_BOOK_ALIASES[book_key])
    return query_key in book_key or query_key in ref_key

def _library_verse_sort_key(verse):
    book_name = verse.get("book") or _extract_book_from_reference(verse.get("ref"))
    book_key = _normalize_bible_book_name(book_name)
    if book_key in _BIBLE_BOOK_ALIASES:
        book_key = _normalize_bible_book_name(_BIBLE_BOOK_ALIASES[book_key])
    book_index = BIBLE_BOOK_ORDER.get(book_key, 10**6)
    chapter, verse_num = _reference_sort_parts(verse.get("ref"))
    ref = str(verse.get("ref") or "").lower()
    return (book_index, chapter, verse_num, ref)

RESEARCH_TOPIC_MAP = {
    "hope": ["hope", "future", "promised", "endure", "trust"],
    "faith": ["faith", "believe", "belief", "trust", "walk by faith"],
    "peace": ["peace", "calm", "rest", "still", "anxious"],
    "love": ["love", "charity", "beloved", "compassion", "mercy"],
    "wisdom": ["wisdom", "understanding", "discern", "knowledge", "counsel"],
    "salvation": ["salvation", "saved", "redeem", "redemption", "grace"],
    "prayer": ["pray", "prayer", "supplication", "intercede", "petition"],
    "holiness": ["holy", "holiness", "sanctified", "pure", "blameless"],
    "forgiveness": ["forgive", "forgiveness", "pardon", "mercy", "debts"],
    "strength": ["strength", "strong", "power", "mighty", "courage"]
}

DEFAULT_COMMUNITY_ROOMS = [
    {"slug": "general", "name": "General", "description": "Daily conversation and encouragement"},
    {"slug": "prayer", "name": "Prayer", "description": "Prayer requests and prayer support"},
    {"slug": "testimony", "name": "Testimony", "description": "Share what God has done"},
    {"slug": "bible-study", "name": "Bible Study", "description": "Scripture questions and insights"},
    {"slug": "help", "name": "Need Help", "description": "Ask for practical or spiritual support"}
]

def _json_loads_safe(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value)
        return parsed
    except Exception:
        return default

def _table_exists(c, db_type, table_name):
    try:
        if db_type == 'postgres':
            c.execute("""
                SELECT EXISTS(
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = %s
                ) AS exists
            """, (table_name,))
            row = c.fetchone()
            if hasattr(row, 'keys'):
                return bool(row.get('exists'))
            return bool(row[0]) if row else False
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table_name,))
        return c.fetchone() is not None
    except Exception:
        return False

def _schema_ready_token(db_type, name):
    return f"{db_type}:{name}"

def _is_schema_ready(db_type, name):
    token = _schema_ready_token(db_type, name)
    with _SCHEMA_READY_LOCK:
        return token in _SCHEMA_READY_FLAGS

def _mark_schema_ready(db_type, name):
    token = _schema_ready_token(db_type, name)
    with _SCHEMA_READY_LOCK:
        _SCHEMA_READY_FLAGS.add(token)

def _tune_sqlite_connection(conn):
    global SQLITE_TUNING_APPLIED
    try:
        # Apply low-overhead pragmas once per process to reduce lock contention and fsync cost.
        if not SQLITE_TUNING_APPLIED:
            conn.execute("PRAGMA journal_mode=WAL")
            SQLITE_TUNING_APPLIED = True
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-20000")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass

def ensure_performance_indexes(c, db_type):
    statements = [
        "CREATE INDEX IF NOT EXISTS idx_likes_user_ts ON likes(user_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_likes_verse ON likes(verse_id)",
        "CREATE INDEX IF NOT EXISTS idx_saves_user_ts ON saves(user_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_saves_verse ON saves(verse_id)",
        "CREATE INDEX IF NOT EXISTS idx_comments_verse_ts ON comments(verse_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_comments_user_ts ON comments(user_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_comments_deleted ON comments(is_deleted)",
        "CREATE INDEX IF NOT EXISTS idx_community_user_ts ON community_messages(user_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_community_ts ON community_messages(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_reactions_item ON comment_reactions(item_type, item_id)",
        "CREATE INDEX IF NOT EXISTS idx_reactions_item_reaction ON comment_reactions(item_type, item_id, reaction)",
        "CREATE INDEX IF NOT EXISTS idx_replies_parent ON comment_replies(parent_type, parent_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_replies_user ON comment_replies(user_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_replies_deleted ON comment_replies(is_deleted)",
        "CREATE INDEX IF NOT EXISTS idx_collections_user ON collections(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_verse_collections_collection ON verse_collections(collection_id)",
        "CREATE INDEX IF NOT EXISTS idx_inventory_user_equipped ON user_inventory(user_id, equipped)",
        "CREATE INDEX IF NOT EXISTS idx_direct_messages_pair_ts ON direct_messages(sender_id, recipient_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_direct_messages_recipient_unread ON direct_messages(recipient_id, is_read, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_direct_messages_recipient_sender ON direct_messages(recipient_id, sender_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_dm_typing_other_user ON dm_typing(other_id, user_id)",
        "CREATE INDEX IF NOT EXISTS idx_daily_actions_user_date ON daily_actions(user_id, event_date, action)",
        "CREATE INDEX IF NOT EXISTS idx_notifications_user_state ON user_notifications(user_id, is_read, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_presence_seen ON user_presence(last_seen)",
        "CREATE INDEX IF NOT EXISTS idx_research_room_ts ON research_community_messages(room_slug, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_research_user_ts ON research_community_messages(user_id, timestamp)"
    ]

    for stmt in statements:
        try:
            c.execute(stmt)
        except Exception:
            # Optional tables may not exist yet; table-specific ensure functions will retry later.
            pass

def _build_in_clause_params(db_type, values):
    safe_values = [v for v in values if v is not None]
    if not safe_values:
        return None, ()
    placeholder = "%s" if db_type == 'postgres' else "?"
    return ",".join([placeholder] * len(safe_values)), tuple(safe_values)

def ensure_research_feature_tables(c, db_type):
    if _is_schema_ready(db_type, "research_features"):
        return
    if db_type == 'postgres':
        c.execute("""
            CREATE TABLE IF NOT EXISTS reading_plans (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                plan_name TEXT NOT NULL,
                plan_days INTEGER NOT NULL,
                start_date TEXT,
                progress_json JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS verse_highlights (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                verse_id INTEGER NOT NULL,
                color TEXT NOT NULL DEFAULT '#FFD54F',
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, verse_id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS memorization_scores (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                verse_id INTEGER NOT NULL,
                accuracy REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 1,
                best_accuracy REAL NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, verse_id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS research_community_rooms (
                id SERIAL PRIMARY KEY,
                slug TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS research_community_messages (
                id SERIAL PRIMARY KEY,
                room_slug TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                timestamp TEXT,
                google_name TEXT,
                google_picture TEXT
            )
        """)
        for room in DEFAULT_COMMUNITY_ROOMS:
            c.execute("""
                INSERT INTO research_community_rooms (slug, name, description)
                VALUES (%s, %s, %s)
                ON CONFLICT (slug) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description
            """, (room["slug"], room["name"], room["description"]))
    else:
        c.execute("""
            CREATE TABLE IF NOT EXISTS reading_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_name TEXT NOT NULL,
                plan_days INTEGER NOT NULL,
                start_date TEXT,
                progress_json TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS verse_highlights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                verse_id INTEGER NOT NULL,
                color TEXT NOT NULL DEFAULT '#FFD54F',
                note TEXT,
                created_at TEXT,
                UNIQUE(user_id, verse_id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS memorization_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                verse_id INTEGER NOT NULL,
                accuracy REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 1,
                best_accuracy REAL NOT NULL DEFAULT 0,
                updated_at TEXT,
                UNIQUE(user_id, verse_id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS research_community_rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS research_community_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_slug TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                timestamp TEXT,
                google_name TEXT,
                google_picture TEXT
            )
        """)
        for room in DEFAULT_COMMUNITY_ROOMS:
            c.execute("""
                INSERT INTO research_community_rooms (slug, name, description)
                VALUES (?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description
            """, (room["slug"], room["name"], room["description"]))
    ensure_performance_indexes(c, db_type)
    _mark_schema_ready(db_type, "research_features")

def _parse_reference_chapter(reference):
    ref = str(reference or "")
    match = re.search(r"\s(\d+)(?:\s*:\s*(\d+))?", ref)
    if not match:
        return (10**9, 10**9)
    chapter = int(match.group(1))
    verse_num = int(match.group(2)) if match.group(2) else 0
    return (chapter, verse_num)

def _normalize_mem_text(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", str(text or "").lower())).strip()

def _build_memorization_mask(text):
    words = re.findall(r"[A-Za-z0-9']+|[^A-Za-z0-9'\s]+|\s+", str(text or ""))
    candidates = [w for w in words if re.match(r"[A-Za-z0-9']+$", w)]
    hide_count = max(1, int(len(candidates) * 0.35))
    hidden = 0
    masked = []
    for token in words:
        if hidden < hide_count and re.match(r"[A-Za-z0-9']+$", token) and len(token) > 2:
            masked.append("_" * len(token))
            hidden += 1
        else:
            masked.append(token)
    return "".join(masked)

def _compute_text_similarity(a, b):
    aa = _normalize_mem_text(a)
    bb = _normalize_mem_text(b)
    if not aa and not bb:
        return 1.0
    if not aa or not bb:
        return 0.0
    a_tokens = aa.split(" ")
    b_tokens = bb.split(" ")
    if not a_tokens or not b_tokens:
        return 0.0
    hits = 0
    b_pool = set(b_tokens)
    for token in a_tokens:
        if token in b_pool:
            hits += 1
    return max(0.0, min(1.0, hits / max(1, len(a_tokens))))

def _dedupe_verses_in_db(c, db_type):
    if db_type == 'postgres':
        c.execute("""
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY LOWER(TRIM(COALESCE(reference, ''))), LOWER(TRIM(COALESCE(text, '')))
                           ORDER BY id ASC
                       ) AS rn
                FROM verses
            )
            DELETE FROM verses v
            USING ranked r
            WHERE v.id = r.id AND r.rn > 1
        """)
        deleted = c.rowcount if c.rowcount and c.rowcount > 0 else 0
        return deleted
    c.execute("""
        DELETE FROM verses
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY LOWER(TRIM(IFNULL(reference, ''))), LOWER(TRIM(IFNULL(text, '')))
                           ORDER BY id ASC
                       ) AS rn
                FROM verses
            ) t
            WHERE t.rn > 1
        )
    """)
    return c.rowcount if c.rowcount and c.rowcount > 0 else 0

def _remove_orphan_verse_refs(c, db_type):
    if db_type == 'postgres':
        c.execute("SELECT COUNT(*) AS count FROM likes l LEFT JOIN verses v ON l.verse_id = v.id WHERE v.id IS NULL")
        row = c.fetchone()
        likes_before = int(row['count'] if hasattr(row, 'keys') else row[0])
        c.execute("SELECT COUNT(*) AS count FROM saves s LEFT JOIN verses v ON s.verse_id = v.id WHERE v.id IS NULL")
        row = c.fetchone()
        saves_before = int(row['count'] if hasattr(row, 'keys') else row[0])

        c.execute("DELETE FROM likes WHERE verse_id NOT IN (SELECT id FROM verses)")
        c.execute("DELETE FROM saves WHERE verse_id NOT IN (SELECT id FROM verses)")
        return {"likes_removed": likes_before, "saves_removed": saves_before}

    c.execute("SELECT COUNT(*) FROM likes WHERE verse_id NOT IN (SELECT id FROM verses)")
    row = c.fetchone()
    likes_before = int(row[0] if row else 0)
    c.execute("SELECT COUNT(*) FROM saves WHERE verse_id NOT IN (SELECT id FROM verses)")
    row = c.fetchone()
    saves_before = int(row[0] if row else 0)
    c.execute("DELETE FROM likes WHERE verse_id NOT IN (SELECT id FROM verses)")
    c.execute("DELETE FROM saves WHERE verse_id NOT IN (SELECT id FROM verses)")
    return {"likes_removed": likes_before, "saves_removed": saves_before}

def get_public_url():
    base = os.environ.get('PUBLIC_URL') or os.environ.get('RENDER_EXTERNAL_URL')
    if base:
        return base.rstrip('/')
    try:
        return request.url_root.rstrip('/')
    except Exception:
        return PUBLIC_URL.rstrip('/')

def get_db():
    """Get database connection - PostgreSQL for Render, SQLite for local"""
    global POSTGRES_AVAILABLE
    if FORCE_SQLITE:
        conn = sqlite3.connect(SQLITE_PATH, timeout=20)
        conn.row_factory = sqlite3.Row
        _tune_sqlite_connection(conn)
        return conn, 'sqlite'
    if FORCE_POSTGRES and not IS_POSTGRES:
        raise RuntimeError("DB_MODE=postgres but DATABASE_URL is not set to a postgres URL")
    if IS_POSTGRES and POSTGRES_AVAILABLE:
        try:
            import psycopg2
            import psycopg2.extras
            conn = psycopg2.connect(DATABASE_URL, sslmode='require')
            return conn, 'postgres'
        except ImportError:
            if STRICT_DB or RENDER_ENV:
                logger.error("psycopg2 not installed and strict DB mode enabled")
                raise
            logger.warning("psycopg2 not installed, falling back to SQLite")
            conn = sqlite3.connect(SQLITE_PATH, timeout=20)
            conn.row_factory = sqlite3.Row
            _tune_sqlite_connection(conn)
            return conn, 'sqlite'
        except Exception as e:
            logger.error(f"PostgreSQL connection failed: {e}")
            if STRICT_DB or RENDER_ENV:
                raise
            POSTGRES_AVAILABLE = False
            # Fallback to SQLite if Postgres fails
            conn = sqlite3.connect(SQLITE_PATH, timeout=20)
            conn.row_factory = sqlite3.Row
            _tune_sqlite_connection(conn)
            return conn, 'sqlite'
    else:
        conn = sqlite3.connect(SQLITE_PATH, timeout=20)
        conn.row_factory = sqlite3.Row
        _tune_sqlite_connection(conn)
        return conn, 'sqlite'

def get_cursor(conn, db_type):
    """Get cursor with dict access"""
    if db_type == 'postgres':
        import psycopg2.extras
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        return conn.cursor()

def row_value(row, key, default=None):
    """Safely read a key from dict-like or sqlite3.Row results."""
    try:
        if isinstance(row, dict):
            return row.get(key, default)
        if hasattr(row, 'keys'):
            keys = row.keys()
            if key in keys:
                return row[key]
    except Exception:
        pass
    return default

def row_pick(row, key, index=None, default=None):
    """Safely read from dict-like rows and fallback to positional index."""
    val = row_value(row, key, None)
    if val is not None:
        return val
    if row is None:
        return default
    if index is not None:
        try:
            return row[index]
        except Exception:
            return default
    return default

def parse_duration_to_seconds(duration_value, default_seconds=3600):
    """Parse duration values like '24h', '30m', '30 min', '1.5h', '2d' into seconds."""
    if duration_value is None:
        return int(default_seconds)
    if isinstance(duration_value, (int, float)):
        seconds = int(duration_value)
        return seconds if seconds > 0 else int(default_seconds)
    text = str(duration_value).strip().lower()
    if not text:
        return int(default_seconds)
    m = re.match(r'^(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$', text)
    if not m:
        m = re.match(r'^(\d+(?:\.\d+)?)([smhd])$', text)
    if not m:
        return int(default_seconds)
    value = float(m.group(1))
    unit = str(m.group(2) or '').strip().lower()
    if unit in ('s', 'sec', 'secs', 'second', 'seconds'):
        factor = 1
    elif unit in ('m', 'min', 'mins', 'minute', 'minutes'):
        factor = 60
    elif unit in ('h', 'hr', 'hrs', 'hour', 'hours'):
        factor = 3600
    elif unit in ('d', 'day', 'days'):
        factor = 86400
    else:
        factor = 1
    seconds = int(value * factor)
    return seconds if seconds > 0 else int(default_seconds)

def ensure_active_boosts_schema(c, db_type):
    """Backfill missing columns for legacy user_active_boosts schemas."""
    try:
        if db_type == 'postgres':
            c.execute("ALTER TABLE user_active_boosts ADD COLUMN IF NOT EXISTS item_id TEXT")
            c.execute("ALTER TABLE user_active_boosts ADD COLUMN IF NOT EXISTS multiplier INTEGER NOT NULL DEFAULT 1")
            c.execute("ALTER TABLE user_active_boosts ADD COLUMN IF NOT EXISTS started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
            c.execute("ALTER TABLE user_active_boosts ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP")
            try:
                c.execute("UPDATE user_active_boosts SET multiplier = 1 WHERE multiplier IS NULL OR multiplier < 1")
            except Exception:
                pass
        else:
            c.execute("PRAGMA table_info(user_active_boosts)")
            cols = {str(r[1]).lower() for r in c.fetchall()}
            if 'item_id' not in cols:
                c.execute("ALTER TABLE user_active_boosts ADD COLUMN item_id TEXT")
            if 'multiplier' not in cols:
                c.execute("ALTER TABLE user_active_boosts ADD COLUMN multiplier INTEGER NOT NULL DEFAULT 1")
            if 'started_at' not in cols:
                c.execute("ALTER TABLE user_active_boosts ADD COLUMN started_at TEXT")
            if 'expires_at' not in cols:
                c.execute("ALTER TABLE user_active_boosts ADD COLUMN expires_at TEXT")
            try:
                c.execute("UPDATE user_active_boosts SET multiplier = 1 WHERE multiplier IS NULL OR multiplier < 1")
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Could not migrate user_active_boosts schema: {e}")

def _coerce_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(text)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    return None

def get_active_boost(c, db_type, user_id, cleanup_expired=True):
    """Return active boost for user or None."""
    if not user_id:
        return None
    try:
        ensure_active_boosts_schema(c, db_type)
        if db_type == 'postgres':
            c.execute("""
                SELECT item_id, multiplier, started_at, expires_at
                FROM user_active_boosts
                WHERE user_id = %s
                LIMIT 1
            """, (user_id,))
        else:
            c.execute("""
                SELECT item_id, multiplier, started_at, expires_at
                FROM user_active_boosts
                WHERE user_id = ?
                LIMIT 1
            """, (user_id,))
        row = c.fetchone()
        if not row:
            return None

        item_id = row_pick(row, 'item_id', 0)
        multiplier = int(row_pick(row, 'multiplier', 1, 1) or 1)
        started_at = _coerce_datetime(row_pick(row, 'started_at', 2))
        expires_at = _coerce_datetime(row_pick(row, 'expires_at', 3))
        if not expires_at:
            return None

        # Treat legacy naive timestamps as UTC to avoid client-side timezone drift.
        expires_at_calc = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
        started_at_calc = started_at if (started_at and started_at.tzinfo) else (started_at.replace(tzinfo=timezone.utc) if started_at else None)
        now = datetime.now(expires_at_calc.tzinfo)
        if expires_at_calc <= now:
            if cleanup_expired:
                if db_type == 'postgres':
                    c.execute("DELETE FROM user_active_boosts WHERE user_id = %s", (user_id,))
                else:
                    c.execute("DELETE FROM user_active_boosts WHERE user_id = ?", (user_id,))
            return None

        remaining = max(0, int((expires_at_calc - now).total_seconds()))
        return {
            "item_id": item_id,
            "multiplier": max(1, multiplier),
            "started_at": started_at_calc.isoformat() if started_at_calc else None,
            "expires_at": expires_at_calc.isoformat(),
            "remaining_seconds": remaining
        }
    except Exception:
        return None

def _redact_db_url(url):
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname or ''
        port = f":{parsed.port}" if parsed.port else ''
        path = parsed.path or ''
        return f"{parsed.scheme}://{host}{port}{path}"
    except Exception:
        return ''

def _list_tables(conn, db_type):
    c = conn.cursor()
    if db_type == 'postgres':
        c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        return [r[0] for r in c.fetchall()]
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return [r[0] for r in c.fetchall()]

def _table_columns(conn, db_type, table):
    c = conn.cursor()
    if db_type == 'postgres':
        c.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
        return [r[0] for r in c.fetchall()]
    c.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in c.fetchall()]

def read_system_setting(key, default=None):
    conn = None
    try:
        conn, db_type = get_db()
        c = get_cursor(conn, db_type)
        c.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        if db_type == 'postgres':
            c.execute("SELECT value FROM system_settings WHERE key = %s", (key,))
        else:
            c.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
        row = c.fetchone()
        conn.close()
        if not row:
            return default
        if hasattr(row, 'keys'):
            try:
                val = row['value']
            except Exception:
                val = None
            return default if val is None else val
        return row[0] if row[0] is not None else default
    except Exception:
        if conn:
            try:
                conn.close()
            except:
                pass
        return default

def init_db():
    """Initialize database tables"""
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        if db_type == 'postgres':
            c.execute('''
                CREATE TABLE IF NOT EXISTS verses (
                    id SERIAL PRIMARY KEY, reference TEXT, text TEXT, 
                    translation TEXT, source TEXT, timestamp TEXT, book TEXT
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY, google_id TEXT UNIQUE, email TEXT, 
                    name TEXT, picture TEXT, created_at TEXT, is_admin INTEGER DEFAULT 0,
                    is_banned BOOLEAN DEFAULT FALSE, ban_expires_at TIMESTAMP, ban_reason TEXT, role TEXT DEFAULT 'user',
                    custom_picture TEXT, avatar_decoration TEXT
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS likes (
                    id SERIAL PRIMARY KEY, user_id INTEGER, verse_id INTEGER, 
                    timestamp TEXT, UNIQUE(user_id, verse_id)
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS saves (
                    id SERIAL PRIMARY KEY, user_id INTEGER, verse_id INTEGER, 
                    timestamp TEXT, UNIQUE(user_id, verse_id)
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS comments (
                    id SERIAL PRIMARY KEY, user_id INTEGER, verse_id INTEGER,
                    text TEXT, timestamp TEXT, google_name TEXT, google_picture TEXT,
                    is_deleted INTEGER DEFAULT 0
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS collections (
                    id SERIAL PRIMARY KEY, user_id INTEGER, name TEXT, 
                    color TEXT, created_at TEXT
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS verse_collections (
                    id SERIAL PRIMARY KEY, collection_id INTEGER, verse_id INTEGER,
                    UNIQUE(collection_id, verse_id)
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS community_messages (
                    id SERIAL PRIMARY KEY, user_id INTEGER, text TEXT, 
                    timestamp TEXT, google_name TEXT, google_picture TEXT
                )
            ''')

            c.execute('''
                CREATE TABLE IF NOT EXISTS comment_reactions (
                    id SERIAL PRIMARY KEY,
                    item_type TEXT NOT NULL,
                    item_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    reaction TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(item_type, item_id, user_id, reaction)
                )
            ''')

            c.execute('''
                CREATE TABLE IF NOT EXISTS comment_replies (
                    id SERIAL PRIMARY KEY,
                    parent_type TEXT NOT NULL,
                    parent_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    google_name TEXT,
                    google_picture TEXT,
                    is_deleted INTEGER DEFAULT 0
                )
            ''')

            c.execute('''
                CREATE TABLE IF NOT EXISTS daily_actions (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    verse_id INTEGER,
                    event_date TEXT NOT NULL,
                    timestamp TEXT,
                    UNIQUE(user_id, action, verse_id, event_date)
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id SERIAL PRIMARY KEY, admin_id TEXT,
                    action TEXT, target_user_id INTEGER, details TEXT,
                    ip_address TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS bans (
                    id SERIAL PRIMARY KEY, user_id INTEGER UNIQUE,
                    reason TEXT, banned_by TEXT, banned_at TIMESTAMP,
                    expires_at TIMESTAMP, ip_address TEXT
                )
            ''')
            
            # User activity logs for comprehensive audit trail
            c.execute('''
                CREATE TABLE IF NOT EXISTS user_activity_logs (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    google_id TEXT,
                    email TEXT,
                    action TEXT NOT NULL,
                    details TEXT,
                    ip_address TEXT,
                    user_agent TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # User signup tracking for first-time sign-in enforcement
            c.execute('''
                CREATE TABLE IF NOT EXISTS user_signup_logs (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER UNIQUE NOT NULL,
                    google_id TEXT UNIQUE NOT NULL,
                    email TEXT NOT NULL,
                    name TEXT,
                    first_signup_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_login_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    signup_ip TEXT,
                    total_logins INTEGER DEFAULT 1
                )
            ''')
            
            # Shop and XP system tables
            c.execute('''
                CREATE TABLE IF NOT EXISTS user_xp (
                    user_id INTEGER PRIMARY KEY,
                    xp INTEGER DEFAULT 0,
                    total_xp_earned INTEGER DEFAULT 0,
                    level INTEGER DEFAULT 1,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS shop_items (
                    id SERIAL PRIMARY KEY,
                    item_id TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    category TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    rarity TEXT DEFAULT 'common',
                    effects JSONB,
                    icon TEXT,
                    available BOOLEAN DEFAULT TRUE
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS user_inventory (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    equipped BOOLEAN DEFAULT FALSE,
                    UNIQUE(user_id, item_id)
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS xp_transactions (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    description TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Bible Learning XP Tables
            c.execute('''
                CREATE TABLE IF NOT EXISTS verse_read_streak (
                    user_id INTEGER PRIMARY KEY,
                    current_streak INTEGER DEFAULT 0,
                    longest_streak INTEGER DEFAULT 0,
                    last_read_date DATE,
                    total_verses_read INTEGER DEFAULT 0
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS verse_memorized (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    verse_id INTEGER NOT NULL,
                    memorized_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    review_count INTEGER DEFAULT 0,
                    last_reviewed TIMESTAMP,
                    UNIQUE(user_id, verse_id)
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS bible_study_notes (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    verse_id INTEGER,
                    book TEXT,
                    chapter INTEGER,
                    note_text TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS prayer_journal (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    prayer_title TEXT,
                    prayer_content TEXT NOT NULL,
                    is_answered BOOLEAN DEFAULT FALSE,
                    answered_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS reading_progress (
                    user_id INTEGER PRIMARY KEY,
                    current_book TEXT DEFAULT 'Genesis',
                    current_chapter INTEGER DEFAULT 1,
                    total_chapters_read INTEGER DEFAULT 0,
                    books_completed TEXT DEFAULT '[]',
                    last_read_at TIMESTAMP
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS bible_trivia_scores (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    questions_answered INTEGER DEFAULT 0,
                    correct_answers INTEGER DEFAULT 0,
                    best_streak INTEGER DEFAULT 0,
                    last_played TIMESTAMP
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS topic_study_progress (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    topic TEXT NOT NULL,
                    verses_studied INTEGER DEFAULT 0,
                    study_time_minutes INTEGER DEFAULT 0,
                    completed BOOLEAN DEFAULT FALSE,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    UNIQUE(user_id, topic)
                )
            ''')
        else:
            # SQLite tables
            c.execute('''CREATE TABLE IF NOT EXISTS verses 
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, reference TEXT, text TEXT, 
                          translation TEXT, source TEXT, timestamp TEXT, book TEXT)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS users 
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, google_id TEXT UNIQUE, email TEXT, 
                          name TEXT, picture TEXT, created_at TEXT, is_admin INTEGER DEFAULT 0,
                          is_banned INTEGER DEFAULT 0, ban_expires_at TEXT, ban_reason TEXT, role TEXT DEFAULT 'user',
                          custom_picture TEXT, avatar_decoration TEXT)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS likes 
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, verse_id INTEGER, 
                          timestamp TEXT, UNIQUE(user_id, verse_id))''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS saves 
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, verse_id INTEGER, 
                          timestamp TEXT, UNIQUE(user_id, verse_id))''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS comments 
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, verse_id INTEGER,
                          text TEXT, timestamp TEXT, google_name TEXT, google_picture TEXT,
                          is_deleted INTEGER DEFAULT 0)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS collections 
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT, 
                          color TEXT, created_at TEXT)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS verse_collections 
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, collection_id INTEGER, verse_id INTEGER,
                          UNIQUE(collection_id, verse_id))''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS community_messages 
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, 
                          timestamp TEXT, google_name TEXT, google_picture TEXT)''')

            c.execute('''CREATE TABLE IF NOT EXISTS comment_reactions
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, item_type TEXT NOT NULL, item_id INTEGER NOT NULL,
                          user_id INTEGER NOT NULL, reaction TEXT NOT NULL, timestamp TEXT,
                          UNIQUE(item_type, item_id, user_id, reaction))''')

            c.execute('''CREATE TABLE IF NOT EXISTS comment_replies
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, parent_type TEXT NOT NULL, parent_id INTEGER NOT NULL,
                          user_id INTEGER NOT NULL, text TEXT NOT NULL, timestamp TEXT, google_name TEXT,
                          google_picture TEXT, is_deleted INTEGER DEFAULT 0)''')

            c.execute('''CREATE TABLE IF NOT EXISTS daily_actions
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, action TEXT NOT NULL,
                          verse_id INTEGER, event_date TEXT NOT NULL, timestamp TEXT,
                          UNIQUE(user_id, action, verse_id, event_date))''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS audit_logs 
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id TEXT,
                          action TEXT, target_user_id INTEGER, details TEXT,
                          ip_address TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS bans 
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER UNIQUE,
                          reason TEXT, banned_by TEXT, banned_at TIMESTAMP,
                          expires_at TIMESTAMP, ip_address TEXT)''')
            
            # User activity logs for comprehensive audit trail
            c.execute('''
                CREATE TABLE IF NOT EXISTS user_activity_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    google_id TEXT,
                    email TEXT,
                    action TEXT NOT NULL,
                    details TEXT,
                    ip_address TEXT,
                    user_agent TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # User signup tracking for first-time sign-in enforcement
            c.execute('''
                CREATE TABLE IF NOT EXISTS user_signup_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE NOT NULL,
                    google_id TEXT UNIQUE NOT NULL,
                    email TEXT NOT NULL,
                    name TEXT,
                    first_signup_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_login_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    signup_ip TEXT,
                    total_logins INTEGER DEFAULT 1
                )
            ''')
            
            # Shop and XP system tables
            c.execute('''
                CREATE TABLE IF NOT EXISTS user_xp (
                    user_id INTEGER PRIMARY KEY,
                    xp INTEGER DEFAULT 0,
                    total_xp_earned INTEGER DEFAULT 0,
                    level INTEGER DEFAULT 1,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS shop_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    category TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    rarity TEXT DEFAULT 'common',
                    effects TEXT,
                    icon TEXT,
                    available BOOLEAN DEFAULT 1
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS user_inventory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    equipped INTEGER DEFAULT 0,
                    UNIQUE(user_id, item_id)
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS xp_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    description TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Bible Learning XP Tables
            c.execute('''
                CREATE TABLE IF NOT EXISTS verse_read_streak (
                    user_id INTEGER PRIMARY KEY,
                    current_streak INTEGER DEFAULT 0,
                    longest_streak INTEGER DEFAULT 0,
                    last_read_date DATE,
                    total_verses_read INTEGER DEFAULT 0
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS verse_memorized (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    verse_id INTEGER NOT NULL,
                    memorized_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    review_count INTEGER DEFAULT 0,
                    last_reviewed TIMESTAMP,
                    UNIQUE(user_id, verse_id)
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS bible_study_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    verse_id INTEGER,
                    book TEXT,
                    chapter INTEGER,
                    note_text TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS prayer_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    prayer_title TEXT,
                    prayer_content TEXT NOT NULL,
                    is_answered INTEGER DEFAULT 0,
                    answered_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS reading_progress (
                    user_id INTEGER PRIMARY KEY,
                    current_book TEXT DEFAULT 'Genesis',
                    current_chapter INTEGER DEFAULT 1,
                    total_chapters_read INTEGER DEFAULT 0,
                    books_completed TEXT DEFAULT '[]',
                    last_read_at TIMESTAMP
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS bible_trivia_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    questions_answered INTEGER DEFAULT 0,
                    correct_answers INTEGER DEFAULT 0,
                    best_streak INTEGER DEFAULT 0,
                    last_played TIMESTAMP
                )
            ''')
            
            c.execute('''
                CREATE TABLE IF NOT EXISTS topic_study_progress (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    topic TEXT NOT NULL,
                    verses_studied INTEGER DEFAULT 0,
                    study_time_minutes INTEGER DEFAULT 0,
                    completed INTEGER DEFAULT 0,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    UNIQUE(user_id, topic)
                )
            ''')
        
        ensure_performance_indexes(c, db_type)
        conn.commit()
        logger.info(f"Database initialized ({db_type})")
    except Exception as e:
        logger.error(f"DB Init Error: {e}")
    finally:
        conn.close()

def migrate_db():
    """Run database migrations to add missing columns"""
    conn, db_type = get_db()
    c = conn.cursor()
    
    try:
        logger.info(f"Running database migrations ({db_type})...")
        
        if db_type == 'postgres':
            # Add is_deleted column to comments table
            try:
                c.execute("ALTER TABLE comments ADD COLUMN IF NOT EXISTS is_deleted INTEGER DEFAULT 0")
                logger.info("Added is_deleted column to comments")
            except Exception as e:
                logger.warning(f"is_deleted column may already exist: {e}")
            
            # Add ip_address column to audit_logs table
            try:
                c.execute("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS ip_address TEXT")
                logger.info("Added ip_address column to audit_logs")
            except Exception as e:
                logger.warning(f"ip_address column may already exist: {e}")
                
            # Add target_user_id column to audit_logs if missing
            try:
                c.execute("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS target_user_id INTEGER")
                logger.info("Added target_user_id column to audit_logs")
            except Exception as e:
                logger.warning(f"target_user_id column may already exist: {e}")
            
            # Create comment_restrictions table if not exists
            try:
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
                logger.info("Created comment_restrictions table")
            except Exception as e:
                logger.warning(f"comment_restrictions table may already exist: {e}")

            try:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS comment_reactions (
                        id SERIAL PRIMARY KEY,
                        item_type TEXT NOT NULL,
                        item_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        reaction TEXT NOT NULL,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(item_type, item_id, user_id, reaction)
                    )
                """)
                logger.info("Created comment_reactions table")
            except Exception as e:
                logger.warning(f"comment_reactions table may already exist: {e}")

            try:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS comment_replies (
                        id SERIAL PRIMARY KEY,
                        parent_type TEXT NOT NULL,
                        parent_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        text TEXT NOT NULL,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        google_name TEXT,
                        google_picture TEXT,
                        is_deleted INTEGER DEFAULT 0
                    )
                """)
                logger.info("Created comment_replies table")
            except Exception as e:
                logger.warning(f"comment_replies table may already exist: {e}")

            try:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS daily_actions (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        action TEXT NOT NULL,
                        verse_id INTEGER,
                        event_date TEXT NOT NULL,
                        timestamp TEXT,
                        UNIQUE(user_id, action, verse_id, event_date)
                    )
                """)
                logger.info("Created daily_actions table")
            except Exception as e:
                logger.warning(f"daily_actions table may already exist: {e}")
            try:
                c.execute("ALTER TABLE daily_actions ADD COLUMN IF NOT EXISTS user_id INTEGER")
                c.execute("ALTER TABLE daily_actions ADD COLUMN IF NOT EXISTS action TEXT")
                c.execute("ALTER TABLE daily_actions ADD COLUMN IF NOT EXISTS verse_id INTEGER")
                c.execute("ALTER TABLE daily_actions ADD COLUMN IF NOT EXISTS event_date TEXT")
                c.execute("ALTER TABLE daily_actions ADD COLUMN IF NOT EXISTS timestamp TEXT")
            except Exception:
                pass

            try:
                c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS custom_picture TEXT")
                c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_decoration TEXT")
            except Exception:
                pass
                
        else:
            # SQLite migrations
            # Check if is_deleted column exists in comments
            try:
                c.execute("SELECT is_deleted FROM comments LIMIT 1")
            except:
                try:
                    c.execute("ALTER TABLE comments ADD COLUMN is_deleted INTEGER DEFAULT 0")
                    logger.info("Added is_deleted column to comments")
                except Exception as e:
                    logger.warning(f"Could not add is_deleted: {e}")
            
            # Check if ip_address column exists in audit_logs
            try:
                c.execute("SELECT ip_address FROM audit_logs LIMIT 1")
            except:
                try:
                    c.execute("ALTER TABLE audit_logs ADD COLUMN ip_address TEXT")
                    logger.info("Added ip_address column to audit_logs")
                except Exception as e:
                    logger.warning(f"Could not add ip_address: {e}")
            
            # Check if target_user_id column exists in audit_logs
            try:
                c.execute("SELECT target_user_id FROM audit_logs LIMIT 1")
            except:
                try:
                    c.execute("ALTER TABLE audit_logs ADD COLUMN target_user_id INTEGER")
                    logger.info("Added target_user_id column to audit_logs")
                except Exception as e:
                    logger.warning(f"Could not add target_user_id: {e}")
            
            # Create comment_restrictions table if not exists
            try:
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
                logger.info("Created comment_restrictions table")
            except Exception as e:
                logger.warning(f"comment_restrictions table may already exist: {e}")

            try:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS comment_reactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        item_type TEXT NOT NULL,
                        item_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        reaction TEXT NOT NULL,
                        timestamp TEXT,
                        UNIQUE(item_type, item_id, user_id, reaction)
                    )
                """)
                logger.info("Created comment_reactions table")
            except Exception as e:
                logger.warning(f"comment_reactions table may already exist: {e}")

            try:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS comment_replies (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        parent_type TEXT NOT NULL,
                        parent_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        text TEXT NOT NULL,
                        timestamp TEXT,
                        google_name TEXT,
                        google_picture TEXT,
                        is_deleted INTEGER DEFAULT 0
                    )
                """)
                logger.info("Created comment_replies table")
            except Exception as e:
                logger.warning(f"comment_replies table may already exist: {e}")

            try:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS daily_actions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        action TEXT NOT NULL,
                        verse_id INTEGER,
                        event_date TEXT NOT NULL,
                        timestamp TEXT,
                        UNIQUE(user_id, action, verse_id, event_date)
                    )
                """)
                logger.info("Created daily_actions table")
            except Exception as e:
                logger.warning(f"daily_actions table may already exist: {e}")
            try:
                c.execute("PRAGMA table_info(daily_actions)")
                cols = {str(r[1]).lower() for r in c.fetchall()}
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
            except Exception as e:
                logger.warning(f"Could not migrate daily_actions columns: {e}")

            try:
                c.execute("PRAGMA table_info(users)")
                ucols = {str(r[1]).lower() for r in c.fetchall()}
                if 'custom_picture' not in ucols:
                    c.execute("ALTER TABLE users ADD COLUMN custom_picture TEXT")
                if 'avatar_decoration' not in ucols:
                    c.execute("ALTER TABLE users ADD COLUMN avatar_decoration TEXT")
            except Exception as e:
                logger.warning(f"Could not migrate user profile columns: {e}")
            
            # Migrate user_activity_logs table (SQLite)
            try:
                c.execute('''
                    CREATE TABLE IF NOT EXISTS user_activity_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        google_id TEXT,
                        email TEXT,
                        action TEXT NOT NULL,
                        details TEXT,
                        ip_address TEXT,
                        user_agent TEXT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                logger.info("Created user_activity_logs table")
            except Exception as e:
                logger.warning(f"user_activity_logs table may already exist: {e}")
            
            # Migrate user_signup_logs table (SQLite)
            try:
                c.execute('''
                    CREATE TABLE IF NOT EXISTS user_signup_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER UNIQUE NOT NULL,
                        google_id TEXT UNIQUE NOT NULL,
                        email TEXT NOT NULL,
                        name TEXT,
                        first_signup_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_login_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        signup_ip TEXT,
                        total_logins INTEGER DEFAULT 1
                    )
                ''')
                logger.info("Created user_signup_logs table")
            except Exception as e:
                logger.warning(f"user_signup_logs table may already exist: {e}")
        
        # Migrate user_activity_logs table (Postgres)
        if db_type == 'postgres':
            try:
                c.execute('''
                    CREATE TABLE IF NOT EXISTS user_activity_logs (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        google_id TEXT,
                        email TEXT,
                        action TEXT NOT NULL,
                        details TEXT,
                        ip_address TEXT,
                        user_agent TEXT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                logger.info("Created user_activity_logs table")
            except Exception as e:
                logger.warning(f"user_activity_logs table may already exist: {e}")
            
            # Migrate user_signup_logs table (Postgres)
            try:
                c.execute('''
                    CREATE TABLE IF NOT EXISTS user_signup_logs (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER UNIQUE NOT NULL,
                        google_id TEXT UNIQUE NOT NULL,
                        email TEXT NOT NULL,
                        name TEXT,
                        first_signup_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_login_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        signup_ip TEXT,
                        total_logins INTEGER DEFAULT 1
                    )
                ''')
                logger.info("Created user_signup_logs table")
            except Exception as e:
                logger.warning(f"user_signup_logs table may already exist: {e}")
            
            # Migrate bans table - add ip_address column (Postgres)
            try:
                c.execute("ALTER TABLE bans ADD COLUMN IF NOT EXISTS ip_address TEXT")
                logger.info("Added ip_address column to bans table")
            except Exception as e:
                logger.warning(f"ip_address column may already exist in bans: {e}")
        
        # Migrate bans table - add ip_address column (SQLite)
        if db_type != 'postgres':
            try:
                c.execute("SELECT ip_address FROM bans LIMIT 1")
            except:
                try:
                    c.execute("ALTER TABLE bans ADD COLUMN ip_address TEXT")
                    logger.info("Added ip_address column to bans table")
                except Exception as e:
                    logger.warning(f"Could not add ip_address to bans: {e}")
        
        # Migrate shop and XP tables (Postgres)
        if db_type == 'postgres':
            try:
                c.execute('''
                    CREATE TABLE IF NOT EXISTS user_xp (
                        user_id INTEGER PRIMARY KEY,
                        xp INTEGER DEFAULT 0,
                        total_xp_earned INTEGER DEFAULT 0,
                        level INTEGER DEFAULT 1,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                c.execute('''
                    CREATE TABLE IF NOT EXISTS shop_items (
                        id SERIAL PRIMARY KEY,
                        item_id TEXT UNIQUE NOT NULL,
                        name TEXT NOT NULL,
                        description TEXT,
                        category TEXT NOT NULL,
                        price INTEGER NOT NULL,
                        rarity TEXT DEFAULT 'common',
                        effects JSONB,
                        icon TEXT,
                        available BOOLEAN DEFAULT TRUE
                    )
                ''')
                c.execute('''
                    CREATE TABLE IF NOT EXISTS user_inventory (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        item_id TEXT NOT NULL,
                        purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        equipped BOOLEAN DEFAULT FALSE,
                        UNIQUE(user_id, item_id)
                    )
                ''')
                c.execute('''
                    CREATE TABLE IF NOT EXISTS xp_transactions (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        amount INTEGER NOT NULL,
                        type TEXT NOT NULL,
                        description TEXT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                logger.info("Created shop and XP tables")
            except Exception as e:
                logger.warning(f"Shop tables may already exist: {e}")
        
        # Migrate shop and XP tables (SQLite)
        if db_type != 'postgres':
            try:
                c.execute('''
                    CREATE TABLE IF NOT EXISTS user_xp (
                        user_id INTEGER PRIMARY KEY,
                        xp INTEGER DEFAULT 0,
                        total_xp_earned INTEGER DEFAULT 0,
                        level INTEGER DEFAULT 1,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                c.execute('''
                    CREATE TABLE IF NOT EXISTS shop_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        item_id TEXT UNIQUE NOT NULL,
                        name TEXT NOT NULL,
                        description TEXT,
                        category TEXT NOT NULL,
                        price INTEGER NOT NULL,
                        rarity TEXT DEFAULT 'common',
                        effects TEXT,
                        icon TEXT,
                        available INTEGER DEFAULT 1
                    )
                ''')
                c.execute('''
                    CREATE TABLE IF NOT EXISTS user_inventory (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        item_id TEXT NOT NULL,
                        purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        equipped INTEGER DEFAULT 0,
                        UNIQUE(user_id, item_id)
                    )
                ''')
                c.execute('''
                    CREATE TABLE IF NOT EXISTS xp_transactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        amount INTEGER NOT NULL,
                        type TEXT NOT NULL,
                        description TEXT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                logger.info("Created shop and XP tables")
            except Exception as e:
                logger.warning(f"Shop tables may already exist: {e}")

        # Ensure inventory supports stackable consumables and active boost tracking.
        if db_type == 'postgres':
            try:
                c.execute("ALTER TABLE user_inventory ADD COLUMN IF NOT EXISTS quantity INTEGER NOT NULL DEFAULT 1")
                c.execute("UPDATE user_inventory SET quantity = 1 WHERE quantity IS NULL OR quantity < 1")
            except Exception as e:
                logger.warning(f"Could not migrate user_inventory quantity (postgres): {e}")
            try:
                c.execute('''
                    CREATE TABLE IF NOT EXISTS user_active_boosts (
                        user_id INTEGER PRIMARY KEY,
                        item_id TEXT NOT NULL,
                        multiplier INTEGER NOT NULL DEFAULT 1,
                        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP NOT NULL
                    )
                ''')
            except Exception as e:
                logger.warning(f"Could not ensure user_active_boosts (postgres): {e}")
        else:
            try:
                c.execute("PRAGMA table_info(user_inventory)")
                inv_cols = {str(r[1]).lower() for r in c.fetchall()}
                if 'quantity' not in inv_cols:
                    c.execute("ALTER TABLE user_inventory ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")
                c.execute("UPDATE user_inventory SET quantity = 1 WHERE quantity IS NULL OR quantity < 1")
            except Exception as e:
                logger.warning(f"Could not migrate user_inventory quantity (sqlite): {e}")
            try:
                c.execute('''
                    CREATE TABLE IF NOT EXISTS user_active_boosts (
                        user_id INTEGER PRIMARY KEY,
                        item_id TEXT NOT NULL,
                        multiplier INTEGER NOT NULL DEFAULT 1,
                        started_at TEXT,
                        expires_at TEXT NOT NULL
                    )
                ''')
            except Exception as e:
                logger.warning(f"Could not ensure user_active_boosts (sqlite): {e}")
        try:
            ensure_active_boosts_schema(c, db_type)
        except Exception as e:
            logger.warning(f"Could not finalize user_active_boosts schema migration: {e}")
        
        ensure_performance_indexes(c, db_type)
        conn.commit()
        logger.info("Database migrations completed")
    except Exception as e:
        logger.error(f"Migration error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        conn.close()

init_db()
migrate_db()

def get_challenge_period_key():
    now = datetime.now().astimezone()
    return now.strftime("%Y-%m-%d")

def get_hour_window():
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)

def ensure_daily_challenge_tables(c, db_type):
    if _is_schema_ready(db_type, "daily_challenge"):
        return
    if db_type == 'postgres':
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_actions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                verse_id INTEGER,
                event_date TEXT NOT NULL,
                timestamp TEXT,
                UNIQUE(user_id, action, verse_id, event_date)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_challenge_claims (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                challenge_date TEXT NOT NULL,
                challenge_id TEXT NOT NULL,
                xp_awarded INTEGER NOT NULL DEFAULT 0,
                claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, challenge_date, challenge_id)
            )
        """)
    else:
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                verse_id INTEGER,
                event_date TEXT NOT NULL,
                timestamp TEXT,
                UNIQUE(user_id, action, verse_id, event_date)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_challenge_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                challenge_date TEXT NOT NULL,
                challenge_id TEXT NOT NULL,
                xp_awarded INTEGER NOT NULL DEFAULT 0,
                claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, challenge_date, challenge_id)
            )
        """)
    ensure_performance_indexes(c, db_type)
    _mark_schema_ready(db_type, "daily_challenge")

def ensure_achievement_tables(c, db_type):
    if _is_schema_ready(db_type, "achievements"):
        return
    if db_type == 'postgres':
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_achievements (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                achievement_id TEXT NOT NULL,
                achievement_name TEXT,
                xp_awarded INTEGER NOT NULL DEFAULT 0,
                unlocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, achievement_id)
            )
        """)
    else:
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                achievement_id TEXT NOT NULL,
                achievement_name TEXT,
                xp_awarded INTEGER NOT NULL DEFAULT 0,
                unlocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, achievement_id)
            )
        """)
    _mark_schema_ready(db_type, "achievements")

def pick_hourly_challenge(user_id, period_key):
    challenges = [
        {
            "id": "easy_save_3",
            "difficulty": "Easy",
            "action": "save",
            "goal": 3,
            "text": "Save 3 verses to your library",
            "xp_min": 800,
            "xp_max": 2200
        },
        {
            "id": "medium_like_8",
            "difficulty": "Medium",
            "action": "like",
            "goal": 8,
            "text": "Like 8 verses",
            "xp_min": 2500,
            "xp_max": 5000
        },
        {
            "id": "hard_save_15",
            "difficulty": "Hard",
            "action": "save",
            "goal": 15,
            "text": "Save 15 verses",
            "xp_min": 6000,
            "xp_max": 14000
        }
    ]
    seed = f"{user_id}:{period_key}"
    value = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)
    return challenges[value % len(challenges)]

def get_hourly_xp_reward(user_id, period_key, challenge=None):
    challenge = challenge or pick_hourly_challenge(user_id, period_key)
    low = max(500, int(challenge.get("xp_min", 500)))
    high = min(10000, int(challenge.get("xp_max", 10000)))
    if low > high:
        low, high = high, low
    seed = f"{user_id}:{period_key}:{challenge.get('id', 'daily')}"
    value = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)
    return low + (value % (high - low + 1))

def record_daily_action(user_id, action, verse_id=None):
    """Persist unique per-window user actions used by the challenge."""
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    period_key = get_challenge_period_key()
    now = datetime.now().isoformat()

    try:
        ensure_daily_challenge_tables(c, db_type)
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO daily_actions (user_id, action, verse_id, event_date, timestamp)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, action, verse_id, event_date) DO NOTHING
            """, (user_id, action, verse_id, period_key, now))
        else:
            c.execute("""
                INSERT OR IGNORE INTO daily_actions (user_id, action, verse_id, event_date, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, action, verse_id, period_key, now))
        conn.commit()
    except Exception as e:
        logger.warning(f"Daily action record failed: {e}")
    finally:
        conn.close()

def log_action(admin_id, action, target_user_id=None, details=None):
    """Log admin actions for audit trail"""
    try:
        from flask import request
        conn, db_type = get_db()
        c = get_cursor(conn, db_type)
        
        try:
            ip = request.remote_addr
        except:
            ip = 'system'
        
        if db_type == 'postgres':
            c.execute("INSERT INTO audit_logs (admin_id, action, target_user_id, details, ip_address) VALUES (%s, %s, %s, %s, %s)",
                      (admin_id, action, target_user_id, json.dumps(details) if details else None, ip))
        else:
            c.execute("INSERT INTO audit_logs (admin_id, action, target_user_id, details, ip_address) VALUES (?, ?, ?, ?, ?)",
                      (admin_id, action, target_user_id, json.dumps(details) if details else None, ip))
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Log error: {e}")

def ensure_comment_social_tables(c, db_type):
    """Ensure reactions/replies tables exist before use."""
    if _is_schema_ready(db_type, "comment_social"):
        return
    if db_type == 'postgres':
        c.execute("""
            CREATE TABLE IF NOT EXISTS comment_reactions (
                id SERIAL PRIMARY KEY,
                item_type TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reaction TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(item_type, item_id, user_id, reaction)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS comment_replies (
                id SERIAL PRIMARY KEY,
                parent_type TEXT NOT NULL,
                parent_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                google_name TEXT,
                google_picture TEXT,
                is_deleted INTEGER DEFAULT 0
            )
        """)
    else:
        c.execute("""
            CREATE TABLE IF NOT EXISTS comment_reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_type TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reaction TEXT NOT NULL,
                timestamp TEXT,
                UNIQUE(item_type, item_id, user_id, reaction)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS comment_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_type TEXT NOT NULL,
                parent_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                timestamp TEXT,
                google_name TEXT,
                google_picture TEXT,
                is_deleted INTEGER DEFAULT 0
            )
        """)
    ensure_performance_indexes(c, db_type)
    _mark_schema_ready(db_type, "comment_social")

def ensure_dm_tables(c, db_type):
    """Ensure direct message tables exist before use."""
    if _is_schema_ready(db_type, "dm"):
        return
    if db_type == 'postgres':
        c.execute("""
            CREATE TABLE IF NOT EXISTS direct_messages (
                id SERIAL PRIMARY KEY,
                sender_id INTEGER NOT NULL,
                recipient_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_read INTEGER DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS dm_typing (
                user_id INTEGER NOT NULL,
                other_id INTEGER NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, other_id)
            )
        """)
    else:
        c.execute("""
            CREATE TABLE IF NOT EXISTS direct_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                recipient_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT,
                is_read INTEGER DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS dm_typing (
                user_id INTEGER NOT NULL,
                other_id INTEGER NOT NULL,
                updated_at TEXT,
                PRIMARY KEY (user_id, other_id)
            )
        """)
    ensure_performance_indexes(c, db_type)
    _mark_schema_ready(db_type, "dm")

def get_reaction_counts(c, db_type, item_type, item_id):
    reactions = {"heart": 0, "pray": 0, "cross": 0}
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
    for row in c.fetchall():
        try:
            key = str(row['reaction']).lower()
            cnt = int(row['cnt'])
        except Exception:
            key = str(row[0]).lower()
            cnt = int(row[1])
        if key in reactions:
            reactions[key] = cnt
    return reactions

def get_reaction_counts_bulk(c, db_type, item_type, item_ids):
    reactions_by_item = {}
    ids_sql, params = _build_in_clause_params(db_type, item_ids)
    if not ids_sql:
        return reactions_by_item

    if db_type == 'postgres':
        c.execute(f"""
            SELECT item_id, reaction, COUNT(*) AS cnt
            FROM comment_reactions
            WHERE item_type = %s AND item_id IN ({ids_sql})
            GROUP BY item_id, reaction
        """, tuple([item_type, *params]))
    else:
        c.execute(f"""
            SELECT item_id, reaction, COUNT(*) AS cnt
            FROM comment_reactions
            WHERE item_type = ? AND item_id IN ({ids_sql})
            GROUP BY item_id, reaction
        """, tuple([item_type, *params]))

    for row in c.fetchall():
        try:
            item_id = int(row['item_id'])
            key = str(row['reaction']).lower()
            cnt = int(row['cnt'])
        except Exception:
            item_id = int(row[0])
            key = str(row[1]).lower()
            cnt = int(row[2])
        bucket = reactions_by_item.setdefault(item_id, {"heart": 0, "pray": 0, "cross": 0})
        if key in bucket:
            bucket[key] = cnt

    for item_id in item_ids:
        if item_id is None:
            continue
        item_int = int(item_id)
        reactions_by_item.setdefault(item_int, {"heart": 0, "pray": 0, "cross": 0})
    return reactions_by_item

def get_replies_for_parent(c, db_type, parent_type, parent_id, equipped_cache=None):
    if db_type == 'postgres':
        c.execute("""
            SELECT
                r.id, r.user_id, r.text, r.timestamp, r.google_name, r.google_picture,
                u.name AS db_name, COALESCE(u.custom_picture, u.picture) AS db_picture, u.role AS db_role,
                u.avatar_decoration AS db_decor
            FROM comment_replies r
            LEFT JOIN users u ON r.user_id = u.id
            WHERE r.parent_type = %s AND r.parent_id = %s AND COALESCE(r.is_deleted, 0) = 0
            ORDER BY r.timestamp ASC
        """, (parent_type, parent_id))
    else:
        c.execute("""
            SELECT
                r.id, r.user_id, r.text, r.timestamp, r.google_name, r.google_picture,
                u.name AS db_name, COALESCE(u.custom_picture, u.picture) AS db_picture, u.role AS db_role,
                u.avatar_decoration AS db_decor
            FROM comment_replies r
            LEFT JOIN users u ON r.user_id = u.id
            WHERE r.parent_type = ? AND r.parent_id = ? AND COALESCE(r.is_deleted, 0) = 0
            ORDER BY r.timestamp ASC
        """, (parent_type, parent_id))
    rows = c.fetchall()
    replies = []
    equipped_cache = equipped_cache if isinstance(equipped_cache, dict) else {}
    for row in rows:
        try:
            reply_id = row['id']
            user_id = row['user_id']
            text = row['text']
            timestamp = row['timestamp']
            google_name = row['google_name']
            google_picture = row['google_picture']
            db_name = row['db_name']
            db_picture = row['db_picture']
            db_role = row['db_role']
            db_decor = row['db_decor']
        except Exception:
            reply_id = row[0]
            user_id = row[1]
            text = row[2]
            timestamp = row[3]
            google_name = row[4]
            google_picture = row[5]
            db_name = row[6] if len(row) > 6 else None
            db_picture = row[7] if len(row) > 7 else None
            db_role = row[8] if len(row) > 8 else None
            db_decor = row[9] if len(row) > 9 else None
        # Get user's equipped items for display (cached per user to avoid repeated queries)
        cache_key = int(user_id) if user_id is not None else None
        if cache_key is not None and cache_key in equipped_cache:
            equipped = equipped_cache[cache_key]
        else:
            equipped = get_user_equipped_items(c, db_type, user_id)
            if cache_key is not None:
                equipped_cache[cache_key] = equipped
        
        replies.append({
            "id": reply_id,
            "user_id": user_id,
            "text": text or "",
            "timestamp": timestamp,
            "user_name": db_name or google_name or "Anonymous",
            "user_picture": db_picture or google_picture or "",
            "user_role": normalize_role(db_role or "user"),
            "avatar_decoration": db_decor or "",
            "equipped_frame": equipped["frame"],
            "equipped_name_color": equipped["name_color"],
            "equipped_title": equipped["title"],
            "equipped_badges": equipped["badges"],
            "equipped_chat_effect": equipped["chat_effect"]
        })
    return replies

def get_replies_for_parents(c, db_type, parent_type, parent_ids, equipped_cache=None):
    replies_map = {}
    ids_sql, params = _build_in_clause_params(db_type, parent_ids)
    if not ids_sql:
        return replies_map

    if db_type == 'postgres':
        c.execute(f"""
            SELECT
                r.id, r.parent_id, r.user_id, r.text, r.timestamp, r.google_name, r.google_picture,
                u.name AS db_name, COALESCE(u.custom_picture, u.picture) AS db_picture, u.role AS db_role,
                u.avatar_decoration AS db_decor
            FROM comment_replies r
            LEFT JOIN users u ON r.user_id = u.id
            WHERE r.parent_type = %s
              AND r.parent_id IN ({ids_sql})
              AND COALESCE(r.is_deleted, 0) = 0
            ORDER BY r.timestamp ASC
        """, tuple([parent_type, *params]))
    else:
        c.execute(f"""
            SELECT
                r.id, r.parent_id, r.user_id, r.text, r.timestamp, r.google_name, r.google_picture,
                u.name AS db_name, COALESCE(u.custom_picture, u.picture) AS db_picture, u.role AS db_role,
                u.avatar_decoration AS db_decor
            FROM comment_replies r
            LEFT JOIN users u ON r.user_id = u.id
            WHERE r.parent_type = ?
              AND r.parent_id IN ({ids_sql})
              AND COALESCE(r.is_deleted, 0) = 0
            ORDER BY r.timestamp ASC
        """, tuple([parent_type, *params]))

    equipped_cache = equipped_cache if isinstance(equipped_cache, dict) else {}
    for row in c.fetchall():
        try:
            reply_id = row['id']
            parent_id = row['parent_id']
            user_id = row['user_id']
            text = row['text']
            timestamp = row['timestamp']
            google_name = row['google_name']
            google_picture = row['google_picture']
            db_name = row['db_name']
            db_picture = row['db_picture']
            db_role = row['db_role']
            db_decor = row['db_decor']
        except Exception:
            reply_id = row[0]
            parent_id = row[1]
            user_id = row[2]
            text = row[3]
            timestamp = row[4]
            google_name = row[5]
            google_picture = row[6]
            db_name = row[7] if len(row) > 7 else None
            db_picture = row[8] if len(row) > 8 else None
            db_role = row[9] if len(row) > 9 else None
            db_decor = row[10] if len(row) > 10 else None

        cache_key = int(user_id) if user_id is not None else None
        if cache_key is not None and cache_key in equipped_cache:
            equipped = equipped_cache[cache_key]
        else:
            equipped = get_user_equipped_items(c, db_type, user_id)
            if cache_key is not None:
                equipped_cache[cache_key] = equipped

        payload = {
            "id": reply_id,
            "user_id": user_id,
            "text": text or "",
            "timestamp": timestamp,
            "user_name": db_name or google_name or "Anonymous",
            "user_picture": db_picture or google_picture or "",
            "user_role": normalize_role(db_role or "user"),
            "avatar_decoration": db_decor or "",
            "equipped_frame": equipped["frame"],
            "equipped_name_color": equipped["name_color"],
            "equipped_title": equipped["title"],
            "equipped_badges": equipped["badges"],
            "equipped_chat_effect": equipped["chat_effect"]
        }
        replies_map.setdefault(int(parent_id), []).append(payload)

    return replies_map

def check_ban_status(user_id):
    """Check if user is currently banned. Returns (is_banned, reason, expires_at)"""
    global BAN_SCHEMA_READY
    if not user_id:
        return (False, None, None)

    now_ts = time.time()
    with _BAN_STATUS_CACHE_LOCK:
        cached = _BAN_STATUS_CACHE.get(int(user_id))
        if cached and (now_ts - cached.get('ts', 0)) < BAN_STATUS_CACHE_TTL:
            return cached.get('value', (False, None, None))

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        # Ensure ban columns exist for older databases (only once per process)
        if not BAN_SCHEMA_READY:
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
                conn.commit()
            except Exception:
                pass
            BAN_SCHEMA_READY = True

        if db_type == 'postgres':
            c.execute("SELECT is_banned, ban_expires_at, ban_reason FROM users WHERE id = %s", (user_id,))
        else:
            c.execute("SELECT is_banned, ban_expires_at, ban_reason FROM users WHERE id = ?", (user_id,))
        
        row = c.fetchone()
        conn.close()
        
        if not row:
            return (False, None, None)
        
        try:
            is_banned = bool(row['is_banned'])
            expires_at = row['ban_expires_at']
            reason = row['ban_reason']
        except (TypeError, KeyError):
            is_banned = bool(row[0])
            expires_at = row[1]
            reason = row[2]
        
        # Check if temporary ban expired
        if is_banned and expires_at:
            try:
                expire_dt = datetime.fromisoformat(str(expires_at))
                if datetime.now() > expire_dt:
                    # Auto-unban
                    conn, db_type = get_db()
                    c = get_cursor(conn, db_type)
                    if db_type == 'postgres':
                        c.execute("UPDATE users SET is_banned = FALSE, ban_expires_at = NULL, ban_reason = NULL WHERE id = %s", (user_id,))
                    else:
                        c.execute("UPDATE users SET is_banned = 0, ban_expires_at = NULL, ban_reason = NULL WHERE id = ?", (user_id,))
                    conn.commit()
                    conn.close()
                    with _BAN_STATUS_CACHE_LOCK:
                        _BAN_STATUS_CACHE[int(user_id)] = {"ts": time.time(), "value": (False, None, None)}
                    return (False, None, None)
            except:
                pass

        result = (is_banned, reason, expires_at)
        with _BAN_STATUS_CACHE_LOCK:
            _BAN_STATUS_CACHE[int(user_id)] = {"ts": time.time(), "value": result}
        return result
    except Exception as e:
        logger.error(f"Ban check error: {e}")
        conn.close()
        return (False, None, None)

def check_ip_ban(ip_address):
    """Check if an IP address is associated with any banned user. Returns (is_banned, reason, original_user_id)"""
    if not ip_address or ip_address == 'unknown':
        return (False, None, None)
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        # Check bans table for this IP
        if db_type == 'postgres':
            c.execute("""
                SELECT b.reason, b.user_id, b.expires_at
                FROM bans b
                WHERE b.ip_address = %s
                AND (b.expires_at IS NULL OR b.expires_at > CURRENT_TIMESTAMP)
                ORDER BY b.banned_at DESC
                LIMIT 1
            """, (ip_address,))
        else:
            now_iso = datetime.now().isoformat()
            c.execute("""
                SELECT b.reason, b.user_id, b.expires_at
                FROM bans b
                WHERE b.ip_address = ?
                AND (b.expires_at IS NULL OR b.expires_at > ?)
                ORDER BY b.banned_at DESC
                LIMIT 1
            """, (ip_address, now_iso))
        
        row = c.fetchone()
        if row:
            try:
                reason = row['reason'] if hasattr(row, 'keys') else row[0]
                user_id = row['user_id'] if hasattr(row, 'keys') else row[1]
            except (TypeError, KeyError):
                reason = row[0]
                user_id = row[1]
            conn.close()
            return (True, reason, user_id)
        
        # Also check user_signup_logs joined with bans
        # This catches cases where the IP wasn't recorded in the ban but the user signed up with it
        if db_type == 'postgres':
            c.execute("""
                SELECT b.reason, b.user_id
                FROM bans b
                JOIN user_signup_logs usl ON b.user_id = usl.user_id
                WHERE usl.signup_ip = %s
                AND (b.expires_at IS NULL OR b.expires_at > CURRENT_TIMESTAMP)
                ORDER BY b.banned_at DESC
                LIMIT 1
            """, (ip_address,))
        else:
            now_iso = datetime.now().isoformat()
            c.execute("""
                SELECT b.reason, b.user_id
                FROM bans b
                JOIN user_signup_logs usl ON b.user_id = usl.user_id
                WHERE usl.signup_ip = ?
                AND (b.expires_at IS NULL OR b.expires_at > ?)
                ORDER BY b.banned_at DESC
                LIMIT 1
            """, (ip_address, now_iso))
        
        row = c.fetchone()
        conn.close()
        
        if row:
            try:
                reason = row['reason'] if hasattr(row, 'keys') else row[0]
                user_id = row['user_id'] if hasattr(row, 'keys') else row[1]
            except (TypeError, KeyError):
                reason = row[0]
                user_id = row[1]
            return (True, reason, user_id)
        
        return (False, None, None)
    except Exception as e:
        logger.error(f"IP ban check error: {e}")
        conn.close()
        return (False, None, None)

def auto_ban_user(user_id, reason, original_user_id=None, ip_address=None):
    """Auto-ban a user for IP-based ban evasion"""
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        auto_reason = f"Auto-banned: IP matches banned user (ID: {original_user_id}). Original reason: {reason}"
        
        if db_type == 'postgres':
            c.execute(
                "UPDATE users SET is_banned = TRUE, ban_expires_at = NULL, ban_reason = %s WHERE id = %s",
                (auto_reason, user_id)
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
            """, (user_id, auto_reason, 'system_auto', datetime.now().isoformat(), None, ip_address))
        else:
            c.execute(
                "UPDATE users SET is_banned = 1, ban_expires_at = NULL, ban_reason = ? WHERE id = ?",
                (auto_reason, user_id)
            )
            c.execute("""
                INSERT OR REPLACE INTO bans (user_id, reason, banned_by, banned_at, expires_at, ip_address)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, auto_reason, 'system_auto', datetime.now().isoformat(), None, ip_address))
        
        conn.commit()
        conn.close()
        
        # Log the auto-ban
        log_action(
            'system_auto',
            'AUTO_BAN_IP',
            target_user_id=user_id,
            details={'reason': auto_reason, 'original_user_id': original_user_id, 'ip_address': ip_address}
        )
        
        logger.info(f"Auto-banned user {user_id} for IP ban evasion (original user: {original_user_id}, IP: {ip_address})")
        return True
    except Exception as e:
        logger.error(f"Auto-ban error: {e}")
        conn.close()
        return False

# Register admin blueprint
from admin import admin_bp
app.register_blueprint(admin_bp)

class BibleGenerator:
    def __init__(self):
        self.running = True
        self.interval = self._load_interval_from_db()
        self.time_left = self.interval
        self.current_verse = None
        self.total_verses = 0
        self.session_id = secrets.token_hex(8)
        self.thread = None
        self.lock = threading.Lock()
        
        # Fallback verses in case API fails
        self.fallback_verses = [
            {"id": 1, "ref": "John 3:16", "text": "For God so loved the world, that he gave his only begotten Son, that whosoever believeth in him should not perish, but have everlasting life.", "trans": "KJV", "source": "Fallback", "book": "John"},
            {"id": 2, "ref": "Philippians 4:13", "text": "I can do all things through Christ which strengtheneth me.", "trans": "KJV", "source": "Fallback", "book": "Philippians"},
            {"id": 3, "ref": "Psalm 23:1", "text": "The LORD is my shepherd; I shall not want.", "trans": "KJV", "source": "Fallback", "book": "Psalm"},
            {"id": 4, "ref": "Romans 8:28", "text": "And we know that all things work together for good to them that love God, to them who are the called according to his purpose.", "trans": "KJV", "source": "Fallback", "book": "Romans"},
            {"id": 5, "ref": "Jeremiah 29:11", "text": "For I know the thoughts that I think toward you, saith the LORD, thoughts of peace, and not of evil, to give you an expected end.", "trans": "KJV", "source": "Fallback", "book": "Jeremiah"}
        ]
        
        # Try to load most recent verse from database first
        try:
            conn, db_type = get_db()
            c = get_cursor(conn, db_type)
            c.execute("SELECT id, reference, text, translation, source, book FROM verses ORDER BY timestamp DESC LIMIT 1")
            row = c.fetchone()
            conn.close()
            
            if row:
                # Use most recent verse from database
                try:
                    verse_id = row['id'] if hasattr(row, 'keys') else row[0]
                    ref = row['reference'] if hasattr(row, 'keys') else row[1]
                    text = row['text'] if hasattr(row, 'keys') else row[2]
                    trans = row['translation'] if hasattr(row, 'keys') else row[3]
                    source = row['source'] if hasattr(row, 'keys') else row[4]
                    book = row['book'] if hasattr(row, 'keys') else row[5]
                except:
                    verse_id, ref, text, trans, source, book = row
                
                self.current_verse = {
                    "id": verse_id,
                    "ref": ref,
                    "text": text,
                    "trans": trans,
                    "source": source,
                    "book": book,
                    "is_new": False,
                    "session_id": self.session_id
                }
                logger.info(f"Loaded verse from database: {ref}")
            else:
                # No verses in database, use fallback
                self.current_verse = random.choice(self.fallback_verses)
                self.current_verse['session_id'] = self.session_id
        except Exception as e:
            logger.error(f"Failed to load verse from DB: {e}")
            # Start with fallback verse
            self.current_verse = random.choice(self.fallback_verses)
            self.current_verse['session_id'] = self.session_id
        
        self.networks = [
            {"name": "Bible-API.com", "url": "https://bible-api.com/?random=verse"},
            {"name": "labs.bible.org", "url": "https://labs.bible.org/api/?passage=random&type=json"},
            {"name": "KJV Random", "url": "https://bible-api.com/?random=verse&translation=kjv"}
        ]
        self.network_idx = 0
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0'})
        
        # Start thread
        self.start_thread()
    
    def _load_interval_from_db(self):
        """Load verse interval from database, default to 60 seconds"""
        try:
            conn, db_type = get_db()
            c = conn.cursor()
            
            # Ensure table exists
            c.execute("""
                CREATE TABLE IF NOT EXISTS system_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            
            # Get verse_interval setting
            c.execute("SELECT value FROM system_settings WHERE key = 'verse_interval'")
            row = c.fetchone()
            conn.close()
            
            if row:
                interval = int(row[0])
                logger.info(f"Loaded verse interval from database: {interval} seconds")
                return interval
        except Exception as e:
            logger.error(f"Failed to load interval from DB: {e}")
        
        return 60  # Default to 60 seconds
    
    def start_thread(self):
        """Start or restart the generator thread"""
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.loop)
            self.thread.daemon = True
            self.thread.start()
            logger.info("BibleGenerator thread started")
    
    def set_interval(self, seconds):
        with self.lock:
            self.interval = max(10, min(3600, int(seconds)))
            self.time_left = min(self.time_left, self.interval)
    
    def extract_book(self, ref):
        match = re.match(r'^([0-9]?\s?[A-Za-z]+)', ref)
        return match.group(1) if match else "Unknown"
    
    def fetch_verse(self):
        """Fetch a new verse from API or use fallback"""
        network = self.networks[self.network_idx]
        verse_data = None
        
        try:
            r = self.session.get(network["url"], timeout=10)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    data = data[0]
                    ref = f"{data['bookname']} {data['chapter']}:{data['verse']}"
                    text = data['text']
                    trans = "WEB"
                else:
                    ref = data.get('reference', 'Unknown')
                    text = data.get('text', '').strip()
                    trans = data.get('translation_name', 'KJV')
                
                if text and ref:
                    book = self.extract_book(ref)
                    verse_data = {
                        "ref": ref,
                        "text": text,
                        "trans": trans,
                        "source": network["name"],
                        "book": book
                    }
        except Exception as e:
            logger.error(f"Fetch error from {network['name']}: {e}")
        
        # Rotate network for next time
        self.network_idx = (self.network_idx + 1) % len(self.networks)
        
        # If API failed, use fallback
        if not verse_data:
            logger.warning("Using fallback verse")
            fallback = random.choice(self.fallback_verses)
            verse_data = {
                "ref": fallback['ref'],
                "text": fallback['text'],
                "trans": fallback['trans'],
                "source": "Fallback",
                "book": fallback['book']
            }
        
        # Store in database
        try:
            conn, db_type = get_db()
            c = get_cursor(conn, db_type)
            
            if db_type == 'postgres':
                c.execute("""
                    INSERT INTO verses (reference, text, translation, source, timestamp, book) 
                    VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                """, (verse_data['ref'], verse_data['text'], verse_data['trans'], 
                      verse_data['source'], datetime.now().isoformat(), verse_data['book']))
            else:
                c.execute("INSERT OR IGNORE INTO verses (reference, text, translation, source, timestamp, book) VALUES (?, ?, ?, ?, ?, ?)",
                          (verse_data['ref'], verse_data['text'], verse_data['trans'], 
                           verse_data['source'], datetime.now().isoformat(), verse_data['book']))
            
            conn.commit()
            
            # Get the ID
            if db_type == 'postgres':
                c.execute("SELECT id FROM verses WHERE reference = %s AND text = %s", 
                         (verse_data['ref'], verse_data['text']))
            else:
                c.execute("SELECT id FROM verses WHERE reference = ? AND text = ?", 
                         (verse_data['ref'], verse_data['text']))
            
            result = c.fetchone()
            try:
                verse_id = result['id'] if result else random.randint(1000, 9999)
            except (TypeError, KeyError):
                verse_id = result[0] if result else random.randint(1000, 9999)
            
            # Update session
            self.session_id = secrets.token_hex(8)
            
            conn.close()
            
            with self.lock:
                self.current_verse = {
                    "id": verse_id,
                    "ref": verse_data['ref'],
                    "text": verse_data['text'],
                    "trans": verse_data['trans'],
                    "source": verse_data['source'],
                    "book": verse_data['book'],
                    "is_new": True,
                    "session_id": self.session_id
                }
                self.total_verses += 1
                
            logger.info(f"New verse fetched: {verse_data['ref']}")
            return True
            
        except Exception as e:
            logger.error(f"Database error in fetch_verse: {e}")
            # Still update current_verse even if DB fails
            with self.lock:
                self.current_verse = {
                    "id": random.randint(1000, 9999),
                    "ref": verse_data['ref'],
                    "text": verse_data['text'],
                    "trans": verse_data['trans'],
                    "source": verse_data['source'],
                    "book": verse_data['book'],
                    "is_new": True,
                    "session_id": secrets.token_hex(8)
                }
            return True
    
    def get_current_verse(self):
        """Thread-safe get current verse"""
        with self.lock:
            return self.current_verse.copy() if self.current_verse else None
    
    def get_time_left(self):
        """Thread-safe get time left"""
        with self.lock:
            return self.time_left
    
    def reset_timer(self):
        """Reset the timer after fetching"""
        with self.lock:
            self.time_left = self.interval
    
    def decrement_timer(self):
        """Decrement timer by 1 second"""
        with self.lock:
            self.time_left -= 1
            return self.time_left
    
    def loop(self):
        """Main loop - runs forever"""
        while self.running:
            try:
                current = self.get_time_left()
                if current <= 0:
                    self.fetch_verse()
                    self.reset_timer()
                else:
                    self.decrement_timer()
            except Exception as e:
                logger.error(f"Critical error in generator loop: {e}")
                time.sleep(5)  # Wait before retrying
                continue
            time.sleep(1)

# Global generator instance
generator = BibleGenerator()
CURRENT_API_CACHE_TTL = max(0.5, float(os.environ.get('API_CURRENT_CACHE_TTL', '2.0')))
_current_api_cache = {}
_current_api_cache_lock = threading.Lock()

# Bind the method to the class
def generate_smart_recommendation(self, user_id, exclude_ids=None):
    """Generate recommendation based on user likes"""
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    exclude_ids = exclude_ids or []
    cleaned_exclude = []
    for item in exclude_ids:
        try:
            cleaned_exclude.append(int(item))
        except (TypeError, ValueError):
            continue
    exclude_ids = list(dict.fromkeys(cleaned_exclude))
    
    try:
        if db_type == 'postgres':
            c.execute("""
                SELECT DISTINCT v.book FROM verses v 
                JOIN likes l ON v.id = l.verse_id 
                WHERE l.user_id = %s
                UNION
                SELECT DISTINCT v.book FROM verses v 
                JOIN saves s ON v.id = s.verse_id 
                WHERE s.user_id = %s
            """, (user_id, user_id))
        else:
            c.execute("""
                SELECT DISTINCT v.book FROM verses v 
                JOIN likes l ON v.id = l.verse_id 
                WHERE l.user_id = ?
                UNION
                SELECT DISTINCT v.book FROM verses v 
                JOIN saves s ON v.id = s.verse_id 
                WHERE s.user_id = ?
            """, (user_id, user_id))
        
        preferred_books = []
        for row in c.fetchall():
            try:
                preferred_books.append(row['book'])
            except (TypeError, KeyError):
                preferred_books.append(row[0])
        
        if preferred_books:
            if db_type == 'postgres':
                placeholders = ','.join(['%s'] * len(preferred_books))
                exclude_clause = ''
                exclude_params = []
                if exclude_ids:
                    exclude_clause = f" AND v.id NOT IN ({','.join(['%s'] * len(exclude_ids))})"
                    exclude_params = exclude_ids
                c.execute(f"""
                    SELECT v.* FROM verses v
                    WHERE v.book IN ({placeholders})
                    AND v.id NOT IN (SELECT verse_id FROM likes WHERE user_id = %s)
                    AND v.id NOT IN (SELECT verse_id FROM saves WHERE user_id = %s)
                    {exclude_clause}
                    ORDER BY RANDOM()
                    LIMIT 1
                """, (*preferred_books, user_id, user_id, *exclude_params))
            else:
                placeholders = ','.join('?' for _ in preferred_books)
                exclude_clause = ''
                exclude_params = []
                if exclude_ids:
                    exclude_clause = f" AND v.id NOT IN ({','.join('?' for _ in exclude_ids)})"
                    exclude_params = exclude_ids
                c.execute(f"""
                    SELECT v.* FROM verses v
                    WHERE v.book IN ({placeholders})
                    AND v.id NOT IN (SELECT verse_id FROM likes WHERE user_id = ?)
                    AND v.id NOT IN (SELECT verse_id FROM saves WHERE user_id = ?)
                    {exclude_clause}
                    ORDER BY RANDOM()
                    LIMIT 1
                """, (*preferred_books, user_id, user_id, *exclude_params))
        else:
            if db_type == 'postgres':
                exclude_clause = ''
                exclude_params = []
                if exclude_ids:
                    exclude_clause = f" AND id NOT IN ({','.join(['%s'] * len(exclude_ids))})"
                    exclude_params = exclude_ids
                c.execute(f"""
                    SELECT * FROM verses 
                    WHERE id NOT IN (SELECT verse_id FROM likes WHERE user_id = %s)
                    AND id NOT IN (SELECT verse_id FROM saves WHERE user_id = %s)
                    {exclude_clause}
                    ORDER BY RANDOM() LIMIT 1
                """, (user_id, user_id, *exclude_params))
            else:
                exclude_clause = ''
                exclude_params = []
                if exclude_ids:
                    exclude_clause = f" AND id NOT IN ({','.join('?' for _ in exclude_ids)})"
                    exclude_params = exclude_ids
                c.execute(f"""
                    SELECT * FROM verses 
                    WHERE id NOT IN (SELECT verse_id FROM likes WHERE user_id = ?)
                    AND id NOT IN (SELECT verse_id FROM saves WHERE user_id = ?)
                    {exclude_clause}
                    ORDER BY RANDOM() LIMIT 1
                """, (user_id, user_id, *exclude_params))
        
        row = c.fetchone()
        
        if row:
            def pick_reason(book_name=None, preferred=False):
                if preferred and book_name:
                    options = [
                        f"Because you like {book_name}",
                        f"A fresh passage from {book_name}",
                        f"Something uplifting from {book_name}",
                        f"More wisdom in {book_name}"
                    ]
                else:
                    options = [
                        "Recommended for you",
                        "A fresh verse for today",
                        "Something to reflect on",
                        "A new verse to explore"
                    ]
                return random.choice(options)
            try:
                return {
                    "id": row['id'], 
                    "ref": row['reference'], 
                    "text": row['text'],
                    "trans": row['translation'], 
                    "book": row['book'],
                    "reason": pick_reason(row['book'], bool(preferred_books))
                }
            except (TypeError, KeyError):
                return {
                    "id": row[0], 
                    "ref": row[1], 
                    "text": row[2],
                    "trans": row[3], 
                    "book": row[6],
                    "reason": pick_reason(row[6], bool(preferred_books))
                }
        return None
    except Exception as e:
        logger.error(f"Recommendation error: {e}")
        return None
    finally:
        conn.close()

BibleGenerator.generate_smart_recommendation = generate_smart_recommendation

@app.before_request
def check_user_banned():
    """Check if current user is banned before processing request"""
    endpoint = request.endpoint or ''
    path = request.path or ''
    public_allow = {'logout', 'check_ban', 'static', 'login', 'google_login', 'callback', 'health', 'manifest', 'serve_audio', 'serve_video'}

    if not path.startswith('/admin') and endpoint not in public_allow:
        maintenance_raw = read_system_setting('maintenance_mode', '0')
        maintenance_enabled = str(maintenance_raw).strip().lower() in ('1', 'true', 'yes', 'on')
        if maintenance_enabled and not session.get('admin_role'):
            if request.is_json or path.startswith('/api/'):
                return jsonify({
                    "error": "maintenance",
                    "message": "Server is currently down due to maintenance. Come back later, thanks for waiting."
                }), 503
            return render_template_string("""
            <!DOCTYPE html>
            <html>
            <head><title>Maintenance</title>
            <style>
                body { background: #0a0a0f; color: white; font-family: system-ui; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
                .box { text-align: center; padding: 40px; background: rgba(10,132,255,0.12); border: 1px solid rgba(10,132,255,0.45); border-radius: 20px; max-width: 520px; }
                h1 { color: #6fa7ff; margin-bottom: 14px; }
            </style></head>
            <body>
                <div class="box">
                    <h1>Maintenance Mode</h1>
                    <p>Server is currently down due to maintenance. Come back later, thanks for waiting.</p>
                </div>
            </body>
            </html>
            """), 503

    if 'user_id' in session:
        # Track user presence for admin analytics.
        try:
            last_ping = session.get('last_presence_ping', 0)
            now_ts = time.time()
            should_ping = True
            try:
                should_ping = (now_ts - float(last_ping)) >= 20
            except Exception:
                should_ping = True
            if should_ping:
                conn, db_type = get_db()
                c = get_cursor(conn, db_type)
                c.execute("""
                    CREATE TABLE IF NOT EXISTS user_presence (
                        user_id INTEGER PRIMARY KEY,
                        last_seen TEXT,
                        last_path TEXT,
                        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                now_iso = datetime.now().isoformat()
                if db_type == 'postgres':
                    c.execute("""
                        INSERT INTO user_presence (user_id, last_seen, last_path, updated_at)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET
                            last_seen = EXCLUDED.last_seen,
                            last_path = EXCLUDED.last_path,
                            updated_at = EXCLUDED.updated_at
                    """, (session['user_id'], now_iso, path, now_iso))
                else:
                    c.execute("""
                        INSERT INTO user_presence (user_id, last_seen, last_path, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            last_seen = excluded.last_seen,
                            last_path = excluded.last_path,
                            updated_at = excluded.updated_at
                    """, (session['user_id'], now_iso, path, now_iso))
                conn.commit()
                conn.close()
                session['last_presence_ping'] = now_ts
        except Exception:
            pass

        if endpoint in public_allow:
            return None

        if request.endpoint in ['logout', 'check_ban', 'static', 'login', 'google_login', 'callback', 'health']:
            return None
        
        is_banned, reason, _ = check_ban_status(session['user_id'])
        if is_banned:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({"error": "banned", "reason": reason, "message": "Your account has been banned"}), 403
            else:
                return render_template_string("""
                <!DOCTYPE html>
                <html>
                <head><title>Account Banned</title>
                <style>
                    body { background: #0a0a0f; color: white; font-family: system-ui; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
                    .ban-container { text-align: center; padding: 40px; background: rgba(255,55,95,0.1); border: 1px solid #ff375f; border-radius: 20px; max-width: 400px; }
                    h1 { color: #ff375f; margin-bottom: 20px; }
                    .reason { background: rgba(0,0,0,0.3); padding: 15px; border-radius: 10px; margin: 20px 0; font-style: italic; }
                    a { color: #0A84FF; text-decoration: none; }
                </style></head>
                <body>
                    <div class="ban-container">
                        <h1>Account Banned</h1>
                        <p>Your account has been suspended.</p>
                        {% if reason %}
                        <div class="reason">Reason: {{ reason }}</div>
                        {% endif %}
                        <p>If you believe this is a mistake, contact support.</p>
                        <p><a href="/logout">Logout</a></p>
                    </div>
                </body>
                </html>
                """, reason=reason), 403

@app.route('/health')
def health_check():
    """Health check endpoint to verify generator is running"""
    try:
        status = {
            "status": "healthy",
            "generator_running": generator.thread.is_alive() if generator.thread else False,
            "current_verse": generator.get_current_verse()['ref'] if generator.get_current_verse() else None,
            "time_left": generator.get_time_left(),
            "interval": generator.interval
        }
        return jsonify(status)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/static/audio/<path:filename>')
def serve_audio(filename):
    return send_from_directory(os.path.join(app.root_path, 'static', 'audio'), filename)

@app.route('/media/videos/<path:filename>')
def serve_video(filename):
    """Serve About videos from multiple safe locations for production compatibility."""
    allowed = {'1.mp4', '2.mp4'}
    if filename not in allowed:
        return jsonify({"error": "not_found"}), 404

    search_dirs = [
        os.path.join(app.root_path, 'static', 'videos'),
        os.path.join(app.root_path, 'static', 'audio'),
        app.root_path,
        os.getcwd()
    ]

    # Fast direct checks first.
    for directory in search_dirs:
        full_path = os.path.join(directory, filename)
        if os.path.isfile(full_path):
            return send_from_directory(directory, filename)

    # Fallback: recursive search under app roots in case Render root differs.
    seen = set()
    for base in [app.root_path, os.getcwd()]:
        if not os.path.isdir(base):
            continue
        for root, _, files in os.walk(base):
            if filename in files:
                abs_path = os.path.join(root, filename)
                if abs_path in seen:
                    continue
                seen.add(abs_path)
                return send_from_directory(root, filename)

    return jsonify({
        "error": "not_found",
        "message": "Video file missing on server",
        "root_path": app.root_path,
        "cwd": os.getcwd()
    }), 404

@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "Bible AI",
        "short_name": "BibleAI",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#000000",
        "theme_color": "#0A84FF",
        "icons": [{"src": "/static/icon.png", "sizes": "192x192"}]
    })

@app.route('/favicon.ico')
def favicon():
    static_dir = os.path.join(app.root_path, 'static')
    favicon_path = os.path.join(static_dir, 'favicon.ico')
    if os.path.isfile(favicon_path):
        return send_from_directory(static_dir, 'favicon.ico', mimetype='image/vnd.microsoft.icon')

    icon_path = os.path.join(static_dir, 'icon.png')
    if os.path.isfile(icon_path):
        return send_from_directory(static_dir, 'icon.png', mimetype='image/png')

    return ('', 204)

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    is_banned, reason, _ = check_ban_status(session['user_id'])
    if is_banned:
        return redirect(url_for('logout'))
    
    # Ensure generator thread is running
    generator.start_thread()
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        if db_type == 'postgres':
            c.execute("SELECT id, name, email, picture, custom_picture, avatar_decoration, created_at, role FROM users WHERE id = %s", (session['user_id'],))
        else:
            c.execute("SELECT id, name, email, picture, custom_picture, avatar_decoration, created_at, role FROM users WHERE id = ?", (session['user_id'],))
        
        user = c.fetchone()
        
        if db_type == 'postgres':
            c.execute("SELECT COUNT(*) as count FROM verses")
            total_verses = c.fetchone()['count']
            c.execute("SELECT COUNT(*) as count FROM likes WHERE user_id = %s", (session['user_id'],))
            liked_count = c.fetchone()['count']
            c.execute("SELECT COUNT(*) as count FROM saves WHERE user_id = %s", (session['user_id'],))
            saved_count = c.fetchone()['count']
        else:
            c.execute("SELECT COUNT(*) as count FROM verses")
            try:
                total_verses = c.fetchone()[0]
            except:
                total_verses = c.fetchone()['count']
            c.execute("SELECT COUNT(*) as count FROM likes WHERE user_id = ?", (session['user_id'],))
            try:
                liked_count = c.fetchone()[0]
            except:
                liked_count = c.fetchone()['count']
            c.execute("SELECT COUNT(*) as count FROM saves WHERE user_id = ?", (session['user_id'],))
            try:
                saved_count = c.fetchone()[0]
            except:
                saved_count = c.fetchone()['count']
        
        if not user:
            session.clear()
            return redirect(url_for('login'))
        
        try:
            custom_picture = user['custom_picture']
            base_picture = user['picture']
            effective_picture = custom_picture or base_picture or ''
            user_dict = {
                "id": user['id'],
                "name": user['name'],
                "email": user['email'],
                "picture": effective_picture,
                "avatar_decoration": user.get('avatar_decoration') if isinstance(user, dict) else None,
                "created_at": user.get('created_at') if isinstance(user, dict) else None,
                "role": user.get('role', 'user') if isinstance(user, dict) else (user[7] if len(user) > 7 else 'user')
            }
        except (TypeError, KeyError):
            custom_picture = user[4] if len(user) > 4 else None
            base_picture = user[3] if len(user) > 3 else None
            effective_picture = custom_picture or base_picture or ''
            user_dict = {
                "id": user[0],
                "name": user[1] if len(user) > 1 else '',
                "email": user[2] if len(user) > 2 else '',
                "picture": effective_picture,
                "avatar_decoration": user[5] if len(user) > 5 else None,
                "created_at": user[6] if len(user) > 6 else None,
                "role": user[7] if len(user) > 7 else 'user'
            }
        
        created_at_val = user_dict.get("created_at")
        if isinstance(created_at_val, datetime):
            created_at_val = created_at_val.isoformat()
        if not created_at_val or str(created_at_val).strip().lower() in ('none', 'null', 'undefined'):
            created_at_val = ''
        user_dict["created_at"] = created_at_val
        
        return render_template('web.html', 
                             user=user_dict,
                             stats={"total_verses": total_verses, "liked": liked_count, "saved": saved_count})
    except Exception as e:
        logger.error(f"Index error: {e}")
        return f"Error loading page: {e}", 500
    finally:
        conn.close()

@app.route('/login')
def login():
    return render_template('login.html')

@app.route('/google-login')
def google_login():
    try:
        google_provider_cfg = requests.get(GOOGLE_DISCOVERY_URL).json()
        authorization_endpoint = google_provider_cfg["authorization_endpoint"]
        callback_url = get_public_url() + "/callback"
        state = secrets.token_urlsafe(16)
        session['oauth_state'] = state
        
        auth_url = (
            f"{authorization_endpoint}"
            f"?client_id={GOOGLE_CLIENT_ID}"
            f"&redirect_uri={callback_url}"
            f"&response_type=code"
            f"&scope=openid%20email%20profile"
            f"&state={state}"
        )
        return redirect(auth_url)
    except Exception as e:
        logger.error(f"Google login error: {e}")
        return f"Error initiating Google login: {str(e)}", 500

@app.route('/callback')
def callback():
    code = request.args.get("code")
    error = request.args.get("error")
    state = request.args.get("state")
    
    if error:
        return f"OAuth Error: {error}. Please check that this URL ({PUBLIC_URL}) is authorized in Google Cloud Console.", 400
    if not code:
        return "No authorization code received", 400
    if state != session.get('oauth_state'):
        return "Invalid state parameter (CSRF protection)", 400
    
    try:
        google_provider_cfg = requests.get(GOOGLE_DISCOVERY_URL).json()
        token_endpoint = google_provider_cfg["token_endpoint"]
        callback_url = get_public_url() + "/callback"
        
        token_response = requests.post(
            token_endpoint,
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": callback_url,
                "grant_type": "authorization_code",
            },
        )
        
        if not token_response.ok:
            error_data = token_response.json()
            error_desc = error_data.get('error_description', 'Unknown error')
            return f"Token exchange failed: {error_desc}. Make sure {callback_url} is in your Google Cloud Console authorized redirect URIs.", 400
        
        tokens = token_response.json()
        access_token = tokens.get("access_token")
        
        userinfo_endpoint = google_provider_cfg["userinfo_endpoint"]
        userinfo_response = requests.get(
            userinfo_endpoint,
            headers={"Authorization": f"Bearer {access_token}"}
        )
        
        if not userinfo_response.ok:
            return "Failed to get user info from Google", 400
        
        userinfo = userinfo_response.json()
        google_id = userinfo['sub']
        email = userinfo['email']
        name = userinfo.get('name', email.split('@')[0])
        picture = userinfo.get('picture', '')
        
        conn, db_type = get_db()
        c = get_cursor(conn, db_type)
        
        if db_type == 'postgres':
            c.execute("SELECT * FROM users WHERE google_id = %s", (google_id,))
        else:
            c.execute("SELECT * FROM users WHERE google_id = ?", (google_id,))
        
        user = c.fetchone()
        is_first_signup = False
        
        # Get client IP early for IP ban checking
        client_ip = request.remote_addr or request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or 'unknown'
        
        # Check if this IP is banned (for new signups)
        ip_banned, ban_reason, original_user_id = check_ip_ban(client_ip)
        
        if not user:
            # First-time user - create new record and mark as first signup
            is_first_signup = True
            if db_type == 'postgres':
                c.execute("INSERT INTO users (google_id, email, name, picture, created_at, is_admin, is_banned, role) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                          (google_id, email, name, picture, datetime.now().isoformat(), 0, False, 'user'))
            else:
                c.execute("INSERT INTO users (google_id, email, name, picture, created_at, is_admin, is_banned, role) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          (google_id, email, name, picture, datetime.now().isoformat(), 0, 0, 'user'))
            conn.commit()
            
            if db_type == 'postgres':
                c.execute("SELECT * FROM users WHERE google_id = %s", (google_id,))
            else:
                c.execute("SELECT * FROM users WHERE google_id = ?", (google_id,))
            user = c.fetchone()
            
            # Get the new user_id
            try:
                user_id = user['id'] if isinstance(user, dict) else user[0]
            except (TypeError, KeyError):
                user_id = user[0]
            
            # If IP is banned, auto-ban this new account
            if ip_banned:
                logger.warning(f"New signup from banned IP detected: user_id={user_id}, ip={client_ip}, original_user={original_user_id}")
                auto_ban_user(user_id, ban_reason, original_user_id, client_ip)
                
                # Show ban page immediately
                conn.close()
                return render_template_string("""
                <!DOCTYPE html>
                <html>
                <head><title>Account Banned</title>
                <style>
                    body { background: #0a0a0f; color: white; font-family: system-ui; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
                    .ban-container { text-align: center; padding: 40px; background: rgba(255,55,95,0.1); border: 1px solid #ff375f; border-radius: 20px; max-width: 420px; }
                    h1 { color: #ff375f; margin-bottom: 20px; }
                    .reason { background: rgba(0,0,0,0.3); padding: 15px; border-radius: 10px; margin: 20px 0; font-style: italic; }
                    a { color: #0A84FF; text-decoration: none; }
                </style></head>
                <body>
                    <div class="ban-container">
                        <h1>Account Banned</h1>
                        <p>Your account has been automatically suspended.</p>
                        <div class="reason">Reason: {{ reason }}</div>
                        <p>This IP address is associated with a previously banned account.</p>
                        <p>If you believe this is a mistake, contact support.</p>
                        <p><a href="/logout">Logout</a></p>
                    </div>
                </body>
                </html>
                """, reason=f"Auto-banned: IP matches banned user. Original: {ban_reason}"), 403
        
        # Get user_id for signup tracking
        try:
            user_id = user['id'] if isinstance(user, dict) else user[0]
        except (TypeError, KeyError):
            user_id = user[0]
        
        # Track signup/login in user_signup_logs for ID retention enforcement
        user_agent = request.headers.get('User-Agent', '')[:500]
        now_iso = datetime.now().isoformat()
        
        if is_first_signup:
            # First-time signup - create permanent record
            if db_type == 'postgres':
                c.execute("""
                    INSERT INTO user_signup_logs 
                    (user_id, google_id, email, name, first_signup_at, last_login_at, signup_ip, total_logins)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (google_id) DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        email = EXCLUDED.email,
                        name = EXCLUDED.name,
                        last_login_at = EXCLUDED.last_login_at,
                        total_logins = user_signup_logs.total_logins + 1
                """, (user_id, google_id, email, name, now_iso, now_iso, client_ip, 1))
            else:
                c.execute("""
                    INSERT INTO user_signup_logs 
                    (user_id, google_id, email, name, first_signup_at, last_login_at, signup_ip, total_logins)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(google_id) DO UPDATE SET
                        user_id = excluded.user_id,
                        email = excluded.email,
                        name = excluded.name,
                        last_login_at = excluded.last_login_at,
                        total_logins = user_signup_logs.total_logins + 1
                """, (user_id, google_id, email, name, now_iso, now_iso, client_ip, 1))
        else:
            # Returning user - update login count and verify ID retention
            if db_type == 'postgres':
                c.execute("""
                    INSERT INTO user_signup_logs 
                    (user_id, google_id, email, name, first_signup_at, last_login_at, signup_ip, total_logins)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (google_id) DO UPDATE SET
                        last_login_at = EXCLUDED.last_login_at,
                        total_logins = user_signup_logs.total_logins + 1
                """, (user_id, google_id, email, name, now_iso, now_iso, client_ip, 1))
                
                # Verify and enforce original user_id if there's a mismatch
                c.execute("SELECT user_id FROM user_signup_logs WHERE google_id = %s", (google_id,))
            else:
                c.execute("""
                    INSERT INTO user_signup_logs 
                    (user_id, google_id, email, name, first_signup_at, last_login_at, signup_ip, total_logins)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(google_id) DO UPDATE SET
                        last_login_at = excluded.last_login_at,
                        total_logins = user_signup_logs.total_logins + 1
                """, (user_id, google_id, email, name, now_iso, now_iso, client_ip, 1))
                
                # Verify and enforce original user_id if there's a mismatch
                c.execute("SELECT user_id FROM user_signup_logs WHERE google_id = ?", (google_id,))
            
            original_record = c.fetchone()
            if original_record:
                try:
                    original_user_id = original_record['user_id'] if isinstance(original_record, dict) else original_record[0]
                    if original_user_id and original_user_id != user_id:
                        # ID mismatch detected - this shouldn't happen but we enforce the original ID
                        logger.warning(f"User ID mismatch for google_id {google_id}: current={user_id}, original={original_user_id}")
                        user_id = original_user_id  # Force use of original ID
                        session['id_mismatch_fixed'] = True
                except (TypeError, KeyError, IndexError):
                    pass
        
        conn.commit()
        conn.close()
        
        # Check ban status against the canonical user_id resolved above.
        is_banned, reason, _ = check_ban_status(user_id)
        if is_banned:
            return render_template_string("""
            <!DOCTYPE html>
            <html>
            <head><title>Account Banned</title>
            <style>
                body { background: #0a0a0f; color: white; font-family: system-ui; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
                .ban-container { text-align: center; padding: 40px; background: rgba(255,55,95,0.1); border: 1px solid #ff375f; border-radius: 20px; max-width: 420px; }
                h1 { color: #ff375f; margin-bottom: 20px; }
                .reason { background: rgba(0,0,0,0.3); padding: 15px; border-radius: 10px; margin: 20px 0; font-style: italic; }
                a { color: #0A84FF; text-decoration: none; }
            </style></head>
            <body>
                <div class="ban-container">
                    <h1>Account Banned</h1>
                    <p>Your account has been suspended.</p>
                    {% if reason %}
                    <div class="reason">Reason: {{ reason }}</div>
                    {% endif %}
                    <p>If you believe this is a mistake, contact support.</p>
                    <p><a href="/logout">Logout</a></p>
                </div>
            </body>
            </html>
            """, reason=reason), 403
        
        session['user_id'] = user_id
        session['google_id'] = google_id
        session['user_name'] = user['name'] if isinstance(user, dict) else user[3]
        session['user_email'] = email
        try:
            custom_pic = user.get('custom_picture') if isinstance(user, dict) else (user[11] if len(user) > 11 else None)
        except Exception:
            custom_pic = None
        session['user_picture'] = (custom_pic or (user['picture'] if isinstance(user, dict) else user[4]))
        try:
            session['avatar_decoration'] = user.get('avatar_decoration') if isinstance(user, dict) else (user[12] if len(user) > 12 else None)
        except Exception:
            session['avatar_decoration'] = None
        session['is_admin'] = bool(user['is_admin']) if isinstance(user, dict) else bool(user[6])
        
        try:
            session_role = user['role'] if isinstance(user, dict) else (user[10] if len(user) > 10 else 'user')
        except (TypeError, KeyError):
            session_role = user[10] if len(user) > 10 else 'user'
        session['role'] = session_role
        # Persist admin session based on stored role so admin code is not required each login.
        if session_role in ('owner', 'co_owner', 'mod', 'host'):
            session['admin_role'] = session_role
            session['is_admin'] = True

        # Log login/signup activity
        if is_first_signup:
            log_user_activity(
                "USER_SIGNUP", 
                user_id=user_id, 
                message="New user signup", 
                extras={
                    "email": email, 
                    "google_id": google_id,
                    "is_first_signup": True,
                    "signup_ip": client_ip
                }
            )
        log_user_activity(
            "USER_LOGIN", 
            user_id=user_id, 
            message="User login", 
            extras={
                "email": email, 
                "google_id": google_id,
                "is_first_signup": is_first_signup,
                "total_logins": 1 if is_first_signup else None
            }
        )
        
        return redirect(url_for('index'))
        
    except Exception as e:
        logger.error(f"Callback error: {e}")
        import traceback
        traceback.print_exc()
        return f"Authentication error: {str(e)}. Please contact support.", 500

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/api/check_ban')
def check_ban():
    if 'user_id' not in session:
        return jsonify({"banned": False})
    
    is_banned, reason, expires_at = check_ban_status(session['user_id'])
    return jsonify({
        "banned": is_banned,
        "reason": reason,
        "expires_at": expires_at
    })

@app.route('/api/restriction_status')
def restriction_status():
    if 'user_id' not in session:
        return jsonify({"restricted": False})
    is_restricted, reason, expires_at = check_comment_restriction(session['user_id'])
    return jsonify({
        "restricted": is_restricted,
        "reason": reason,
        "expires_at": expires_at
    })

@app.route('/api/current')
def get_current():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    user_id = session['user_id']
    now = time.time()
    is_banned, reason, _ = check_ban_status(user_id)
    if is_banned:
        return jsonify({"error": "banned", "message": "Account banned", "reason": reason}), 403
    with _current_api_cache_lock:
        cached = _current_api_cache.get(user_id)
        if cached and (now - cached['timestamp']) < CURRENT_API_CACHE_TTL:
            return jsonify(cached['payload'])

    # Ensure thread is running
    generator.start_thread()

    payload = {
        "verse": generator.get_current_verse(),
        "countdown": generator.get_time_left(),
        "total_verses": generator.total_verses,
        "session_id": generator.session_id,
        "interval": generator.interval
    }
    with _current_api_cache_lock:
        _current_api_cache[user_id] = {
            "timestamp": now,
            "payload": payload
        }
    return jsonify(payload)

@app.route('/api/bible/books')
def bible_books():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    translation = (request.args.get('translation') or DEFAULT_TRANSLATION).lower()
    data = _fetch_json(f"{BIBLE_API_BASE}/data/{translation}")
    if data and isinstance(data, dict) and "books" in data:
        return jsonify({
            "translation": data.get("translation", translation),
            "translation_id": data.get("translation_id", translation),
            "books": data.get("books", [])
        })
    if data and isinstance(data, list):
        return jsonify(data)
    return jsonify({
        "translation": "Fallback",
        "translation_id": translation,
        "books": FALLBACK_BOOKS
    })

@app.route('/api/bible/chapter')
def bible_chapter():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    translation = (request.args.get('translation') or DEFAULT_TRANSLATION).lower()
    book = (request.args.get('book') or 'John').strip()
    chapter = (request.args.get('chapter') or '1').strip()

    if not chapter.isdigit():
        return jsonify({"error": "chapter must be a number"}), 400

    query = f"{book} {chapter}"
    encoded = quote(query)
    data = _fetch_json(f"{BIBLE_API_BASE}/{encoded}?translation={translation}")
    if not data:
        return jsonify({"error": "Unable to load passage"}), 502

    return jsonify({
        "reference": data.get("reference"),
        "translation": data.get("translation_name") or data.get("translation") or translation,
        "translation_id": data.get("translation_id", translation),
        "verses": data.get("verses", []),
        "text": data.get("text", "")
    })

@app.route('/api/bible/compare')
def bible_compare():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    reference = (request.args.get('reference') or '').strip()
    if not reference:
        return jsonify({"error": "reference is required"}), 400

    translations_raw = (request.args.get('translations') or '').strip()
    if translations_raw:
        translations = [t.strip().lower() for t in translations_raw.split(',') if t.strip()]
    else:
        translations = ['web', 'kjv']
    translations = translations[:6]

    results = []
    for trans in translations:
        encoded = quote(reference)
        data = _fetch_json(f"{BIBLE_API_BASE}/{encoded}?translation={trans}")
        if not data:
            results.append({
                "translation_id": trans,
                "translation": trans.upper(),
                "reference": reference,
                "text": "",
                "verses": [],
                "ok": False
            })
            continue
        results.append({
            "translation_id": data.get("translation_id", trans),
            "translation": data.get("translation_name") or data.get("translation") or trans.upper(),
            "reference": data.get("reference", reference),
            "text": data.get("text", ""),
            "verses": data.get("verses", []),
            "ok": True
        })

    return jsonify({
        "reference": reference,
        "translations": results
    })

@app.route('/api/bible/topic-search')
def bible_topic_search():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    topic = (request.args.get('topic') or request.args.get('q') or '').strip().lower()
    if not topic:
        return jsonify({"topic": "", "verses": [], "count": 0})

    keywords = RESEARCH_TOPIC_MAP.get(topic, [])
    if not keywords:
        keywords = [topic]
    limit = max(1, min(300, int(request.args.get('limit', 100))))

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        if db_type == 'postgres':
            predicates = []
            params = []
            for kw in keywords:
                token = f"%{kw.lower()}%"
                predicates.append("(LOWER(COALESCE(text, '')) LIKE %s OR LOWER(COALESCE(reference, '')) LIKE %s OR LOWER(COALESCE(book, '')) LIKE %s)")
                params.extend([token, token, token])
            where_sql = " OR ".join(predicates) if predicates else "TRUE"
            c.execute(f"""
                SELECT id, reference, text, translation, source, timestamp, book
                FROM verses
                WHERE {where_sql}
                ORDER BY id DESC
                LIMIT %s
            """, tuple(params + [limit]))
        else:
            predicates = []
            params = []
            for kw in keywords:
                token = f"%{kw.lower()}%"
                predicates.append("(LOWER(IFNULL(text, '')) LIKE ? OR LOWER(IFNULL(reference, '')) LIKE ? OR LOWER(IFNULL(book, '')) LIKE ?)")
                params.extend([token, token, token])
            where_sql = " OR ".join(predicates) if predicates else "1=1"
            c.execute(f"""
                SELECT id, reference, text, translation, source, timestamp, book
                FROM verses
                WHERE {where_sql}
                ORDER BY id DESC
                LIMIT ?
            """, tuple(params + [limit]))

        rows = c.fetchall()
        verses = []
        seen = set()
        for row in rows:
            try:
                item = {
                    "id": row['id'],
                    "ref": row['reference'],
                    "text": row['text'],
                    "trans": row['translation'],
                    "source": row['source'],
                    "timestamp": row['timestamp'],
                    "book": row['book']
                }
            except Exception:
                item = {
                    "id": row[0],
                    "ref": row[1],
                    "text": row[2],
                    "trans": row[3],
                    "source": row[4],
                    "timestamp": row[5],
                    "book": row[6]
                }
            content_key = (
                _normalize_bible_book_name(item.get("ref")),
                _normalize_mem_text(item.get("text"))
            )
            if content_key in seen:
                continue
            seen.add(content_key)
            verses.append(item)

        verses.sort(key=_library_verse_sort_key)
        return jsonify({"topic": topic, "keywords": keywords, "verses": verses, "count": len(verses)})
    except Exception as e:
        logger.error(f"Topic search error: {e}")
        return jsonify({"error": "topic_search_failed"}), 500
    finally:
        conn.close()

def _pick_book_text_url(formats):
    if not isinstance(formats, dict):
        return None
    preferred = [
        'text/plain; charset=utf-8',
        'text/plain; charset=us-ascii',
        'text/plain'
    ]
    for key in preferred:
        val = formats.get(key)
        if isinstance(val, str) and val.startswith('http'):
            return val
    for key, val in formats.items():
        if isinstance(key, str) and key.startswith('text/plain') and isinstance(val, str) and val.startswith('http'):
            return val
    return None

def _strip_gutenberg_boilerplate(text):
    if not text:
        return ''
    cleaned = text
    start_markers = [
        '*** START OF THE PROJECT GUTENBERG EBOOK',
        '*** START OF THIS PROJECT GUTENBERG EBOOK'
    ]
    end_markers = [
        '*** END OF THE PROJECT GUTENBERG EBOOK',
        '*** END OF THIS PROJECT GUTENBERG EBOOK'
    ]
    for marker in start_markers:
        idx = cleaned.find(marker)
        if idx != -1:
            nl = cleaned.find('\n', idx)
            if nl != -1:
                cleaned = cleaned[nl + 1:]
            break
    for marker in end_markers:
        idx = cleaned.find(marker)
        if idx != -1:
            cleaned = cleaned[:idx]
            break
    cleaned = re.sub(r'\r\n?', '\n', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned

def _fetch_json(url, timeout=12):
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None

def _extract_json(text):
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

def _openai_rank_books(query, books):
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key or not books:
        return None

    candidates = []
    for b in books:
        candidates.append({
            "id": b.get("id"),
            "title": b.get("title"),
            "author": b.get("author"),
            "downloads": b.get("downloads", 0),
            "subjects": (b.get("subjects") or [])[:6]
        })

    system = (
        "You rank public-domain book candidates for a reader. "
        "Return JSON ONLY with key ranked_ids (array of ids). "
        "Heavily prioritize download_count/popularity. Use relevance to the query as a tiebreaker. "
        "Only use ids from the candidate list."
    )
    user = (
        f"Query: {query}\n"
        f"Candidates: {json.dumps(candidates, ensure_ascii=False)}"
    )
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ],
        "temperature": 0.2,
        "max_tokens": 180
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        response = requests.post(OPENAI_API_URL, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        parsed = _extract_json(content) if content else None
        if not parsed:
            return None
        ranked_ids = (
            parsed.get("ranked_ids")
            or parsed.get("rankedIds")
            or parsed.get("ids")
        )
        if not isinstance(ranked_ids, list):
            return None

        id_map = {str(b.get("id")): b for b in books}
        seen = set()
        ranked = []
        for rid in ranked_ids:
            key = str(rid)
            if key in id_map and key not in seen:
                ranked.append(id_map[key])
                seen.add(key)

        for b in books:
            key = str(b.get("id"))
            if key not in seen:
                ranked.append(b)

        return ranked
    except Exception as e:
        logger.info(f"OpenAI book ranker unavailable: {e}")
        return None

def _fallback_bible_picks(topic=None):
    picks = [
        {"reference": "John 1-3", "title": "The Prologue and New Birth", "reason": "Iconic opening on Jesus and salvation."},
        {"reference": "Psalm 23", "title": "The Shepherd Psalm", "reason": "Comforting, widely loved passage."},
        {"reference": "Romans 8", "title": "Life in the Spirit", "reason": "Hope, assurance, and victory."},
        {"reference": "Matthew 5-7", "title": "Sermon on the Mount", "reason": "Core teachings of Jesus."},
        {"reference": "Genesis 1-3", "title": "Creation and the Fall", "reason": "Foundational story of origins."},
        {"reference": "Philippians 4", "title": "Peace and Joy", "reason": "Encouragement and practical faith."},
        {"reference": "Isaiah 53", "title": "Suffering Servant", "reason": "Key prophecy about redemption."},
        {"reference": "Luke 15", "title": "Lost and Found", "reason": "Parables of grace and mercy."},
        {"reference": "Proverbs 3", "title": "Wisdom and Trust", "reason": "Guidance for daily life."},
        {"reference": "Ephesians 2", "title": "Grace and New Life", "reason": "Salvation by grace."},
    ]
    return picks

def _openai_bible_picks(topic):
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        return None

    topic_text = topic.strip() if topic else "popular Bible selections"
    system = (
        "You are a Bible reading guide. Return JSON ONLY with key picks: "
        "an array of objects with fields reference, title, reason. "
        "Use well-known, popular Bible sections. "
        "reference must look like 'John 1-3' or 'Romans 8'. "
        "Provide 8-10 picks. Keep reason under 14 words."
    )
    user = f"Topic: {topic_text}"
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ],
        "temperature": 0.3,
        "max_tokens": 220
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        response = requests.post(OPENAI_API_URL, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        parsed = _extract_json(content) if content else None
        if not parsed:
            return None
        raw_picks = parsed.get("picks")
        if not isinstance(raw_picks, list):
            return None

        cleaned = []
        for item in raw_picks:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("reference") or item.get("ref") or "").strip()
            if not ref:
                continue
            title = str(item.get("title") or "").strip() or ref
            reason = str(item.get("reason") or "").strip()
            cleaned.append({"reference": ref, "title": title, "reason": reason})
        return cleaned[:10]
    except Exception as e:
        logger.info(f"OpenAI bible picks unavailable: {e}")
        return None

@app.route('/api/books/search')
def books_search():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    raw_q = (request.args.get('q') or '').strip()
    popular = (request.args.get('popular') or '').lower() in ('1', 'true', 'yes')
    if popular:
        q = raw_q or 'popular'
    else:
        q = raw_q or 'faith'
        if len(q) < 2:
            q = 'faith'

    try:
        params = {}
        if not popular or raw_q:
            params['search'] = q
        if popular:
            params['sort'] = 'popular'
        resp = requests.get('https://gutendex.com/books', params=params, timeout=15)
        if not resp.ok:
            return jsonify({"error": "book_search_failed"}), 502
        payload = resp.json() or {}
        results = payload.get('results') or []

        if popular and not raw_q:
            q_terms = []
        else:
            q_terms = [t for t in re.split(r'\W+', q.lower()) if t]
        books = []
        for b in results[:20]:
            title = (b.get('title') or '').strip()
            authors = b.get('authors') or []
            author_name = ', '.join([(a.get('name') or '').strip() for a in authors if a.get('name')]) or 'Unknown'
            subjects = b.get('subjects') or []
            formats = b.get('formats') or {}
            text_url = _pick_book_text_url(formats)
            if not text_url:
                continue

            haystack = f"{title} {' '.join(subjects)} {author_name}".lower()
            score = 0
            for term in q_terms:
                if term and term in haystack:
                    score += 2
            if popular and not raw_q:
                score += int((b.get('download_count') or 0) / 250)
            else:
                score += int((b.get('download_count') or 0) / 1000)

            entry = {
                "id": b.get('id'),
                "title": title,
                "author": author_name,
                "downloads": b.get('download_count') or 0,
                "cover": (formats.get('image/jpeg') or formats.get('image/png') or ''),
                "text_url": text_url,
                "ai_score": score,
                "subjects": subjects
            }
            BOOK_META_CACHE[str(entry["id"])] = entry
            books.append(entry)

        ranked = _openai_rank_books(q, books)
        if ranked:
            books = ranked
        else:
            books.sort(key=lambda x: (x["ai_score"], x["downloads"]), reverse=True)

        return jsonify({"query": q, "books": books[:12]})
    except Exception as e:
        logger.error(f"Book search error: {e}")
        return jsonify({"error": "book_search_error"}), 500

@app.route('/api/books/content/<int:book_id>')
def books_content(book_id):
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    key = str(book_id)
    cached = BOOK_TEXT_CACHE.get(key)
    if cached:
        return jsonify(cached)

    try:
        meta = BOOK_META_CACHE.get(key)
        if not meta:
            meta_resp = requests.get(f'https://gutendex.com/books/{book_id}', timeout=15)
            if not meta_resp.ok:
                return jsonify({"error": "book_not_found"}), 404
            b = meta_resp.json() or {}
            formats = b.get('formats') or {}
            meta = {
                "id": book_id,
                "title": (b.get('title') or '').strip() or f'Book {book_id}',
                "author": ', '.join([(a.get('name') or '').strip() for a in (b.get('authors') or []) if a.get('name')]) or 'Unknown',
                "cover": (formats.get('image/jpeg') or formats.get('image/png') or ''),
                "text_url": _pick_book_text_url(formats)
            }
            BOOK_META_CACHE[key] = meta

        text_url = meta.get('text_url')
        if not text_url:
            return jsonify({"error": "book_text_unavailable"}), 404

        text_resp = requests.get(text_url, timeout=20)
        if not text_resp.ok:
            return jsonify({"error": "book_text_fetch_failed"}), 502

        raw = text_resp.text or ''
        cleaned = _strip_gutenberg_boilerplate(raw)
        if len(cleaned) < 200:
            return jsonify({"error": "book_text_too_short"}), 422

        # Keep payload reasonable for client rendering.
        cleaned = cleaned[:800000]

        payload = {
            "id": book_id,
            "title": meta.get('title') or f'Book {book_id}',
            "author": meta.get('author') or 'Unknown',
            "cover": meta.get('cover') or '',
            "text": cleaned
        }
        BOOK_TEXT_CACHE[key] = payload
        return jsonify(payload)
    except Exception as e:
        logger.error(f"Book content error: {e}")
        return jsonify({"error": "book_content_error"}), 500

@app.route('/api/bible/picks')
def bible_picks():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    topic = (request.args.get('topic') or '').strip()
    picks = _openai_bible_picks(topic)
    if not picks:
        picks = _fallback_bible_picks(topic)
    return jsonify({"topic": topic, "picks": picks})

@app.route('/api/set_interval', methods=['POST'])
def set_interval():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    admin_role = (session.get('admin_role') or session.get('role') or 'user').lower()
    if not session.get('is_admin') and admin_role not in ('owner', 'co_owner', 'mod', 'host'):
        return jsonify({"error": "Admin required"}), 403
    
    data = request.get_json() or {}
    interval = data.get('interval', 60)
    
    # Validate interval
    if interval < 10 or interval > 3600:
        return jsonify({"error": "Interval must be between 10 and 3600 seconds"}), 400
    
    # Update generator
    generator.set_interval(interval)
    
    # Save to database for persistence
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        
        # Ensure table exists
        c.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Save interval
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO system_settings (key, value, updated_at)
                VALUES ('verse_interval', %s, CURRENT_TIMESTAMP)
                ON CONFLICT (key) DO UPDATE SET
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
        
        conn.commit()
        conn.close()
        logger.info(f"Verse interval saved to database: {interval} seconds")
    except Exception as e:
        logger.error(f"Failed to save interval to DB: {e}")
    
    return jsonify({"success": True, "interval": generator.interval})

@app.route('/api/user_info')
def get_user_info():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    user_id = session['user_id']

    def parse_dt(value):
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except Exception:
            try:
                return datetime.fromisoformat(str(value).replace(' ', 'T'))
            except Exception:
                return None

    def find_earliest_activity():
        earliest = None
        tables = [
            'likes',
            'saves',
            'comments',
            'community_messages',
            'comment_replies',
            'daily_actions'
        ]
        for table in tables:
            try:
                if db_type == 'postgres':
                    c.execute(f"SELECT MIN(timestamp) FROM {table} WHERE user_id = %s", (user_id,))
                else:
                    c.execute(f"SELECT MIN(timestamp) FROM {table} WHERE user_id = ?", (user_id,))
                row = c.fetchone()
                value = row[0] if row else None
                dt = parse_dt(value)
                if dt and (earliest is None or dt < earliest):
                    earliest = dt
            except Exception:
                continue
        return earliest
    
    try:
        if db_type == 'postgres':
            c.execute("SELECT created_at, is_admin, is_banned, role, name, email, picture, custom_picture, avatar_decoration FROM users WHERE id = %s", (user_id,))
        else:
            c.execute("SELECT created_at, is_admin, is_banned, role, name, email, picture, custom_picture, avatar_decoration FROM users WHERE id = ?", (user_id,))
        
        row = c.fetchone()
        
        if row:
            if isinstance(row, dict) or hasattr(row, 'keys'):
                created_at_val = row['created_at']
                is_admin_val = bool(row['is_admin'])
                is_banned_val = bool(row['is_banned'])
                role_val = row['role'] or 'user'
                name_val = row['name'] or session.get('user_name')
                email_val = row_value(row, 'email', session.get('user_email')) or session.get('user_email')
                base_picture = row_value(row, 'picture')
                custom_picture = row_value(row, 'custom_picture')
                avatar_decoration = row_value(row, 'avatar_decoration')
            else:
                created_at_val = row[0]
                is_admin_val = bool(row[1])
                is_banned_val = bool(row[2])
                role_val = row[3] if row[3] else 'user'
                name_val = row[4] if len(row) > 4 else session.get('user_name')
                email_val = row[5] if len(row) > 5 else session.get('user_email')
                base_picture = row[6] if len(row) > 6 else None
                custom_picture = row[7] if len(row) > 7 else None
                avatar_decoration = row[8] if len(row) > 8 else None

            parsed_created = parse_dt(created_at_val)
            created_at_val = parsed_created.isoformat() if parsed_created else None

            if not created_at_val:
                earliest = find_earliest_activity()
                if earliest:
                    created_at_val = earliest.isoformat()
                else:
                    created_at_val = datetime.now().isoformat()
                try:
                    if db_type == 'postgres':
                        c.execute("UPDATE users SET created_at = %s WHERE id = %s", (created_at_val, user_id))
                    else:
                        c.execute("UPDATE users SET created_at = ? WHERE id = ?", (created_at_val, user_id))
                    conn.commit()
                except Exception:
                    conn.rollback()

            # Sync role from session if it is higher than the stored role
            try:
                session_role = normalize_role(session.get('admin_role') or session.get('role') or 'user')
                db_role = normalize_role(role_val)
                if role_priority(session_role) > role_priority(db_role):
                    is_admin_val = True
                    role_val = session_role
                    if db_type == 'postgres':
                        c.execute("UPDATE users SET role = %s, is_admin = %s WHERE id = %s", (role_val, 1, user_id))
                    else:
                        c.execute("UPDATE users SET role = ?, is_admin = ? WHERE id = ?", (role_val, 1, user_id))
                    conn.commit()
            except Exception:
                conn.rollback()

            effective_picture = custom_picture or base_picture or session.get('user_picture') or ''
            return jsonify({
                "created_at": created_at_val,
                "is_admin": is_admin_val,
                "is_banned": is_banned_val,
                "role": role_val,
                "name": name_val,
                "email": email_val,
                "picture": effective_picture,
                "custom_picture": custom_picture,
                "avatar_decoration": avatar_decoration,
                "session_admin": session.get('is_admin', False)
            })
        return jsonify({
            "created_at": None,
            "is_admin": False,
            "is_banned": False,
            "role": "user",
            "name": session.get('user_name'),
            "email": session.get('user_email'),
            "picture": session.get('user_picture'),
            "avatar_decoration": None
        })
    except Exception as e:
        logger.error(f"User info error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/user/update-name', methods=['POST'])
def update_user_name():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json() or {}
    new_name = (data.get('name') or '').strip()
    if len(new_name) < 2:
        return jsonify({"error": "Name must be at least 2 characters"}), 400
    if len(new_name) > 40:
        return jsonify({"error": "Name must be 40 characters or less"}), 400

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        if db_type == 'postgres':
            c.execute("UPDATE users SET name = %s WHERE id = %s", (new_name, session['user_id']))
        else:
            c.execute("UPDATE users SET name = ? WHERE id = ?", (new_name, session['user_id']))
        conn.commit()
        session['user_name'] = new_name
        return jsonify({"success": True, "name": new_name})
    except Exception as e:
        logger.error(f"Update username error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/verify_role_code', methods=['POST'])
def verify_role_code():
    """Verify role code and assign appropriate role"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    data = request.get_json() or {}
    code = data.get('code', '').strip().upper()
    selected_role = data.get('role', '').strip().lower()
    
    # Normalize role codes to uppercase for comparison
    host_code = str(ROLE_CODES.get('host', '')).strip().upper()
    mod_code = str(ROLE_CODES.get('mod', '')).strip().upper()
    co_owner_code = str(ROLE_CODES.get('co_owner', '')).strip().upper()
    owner_code = str(ROLE_CODES.get('owner', '')).strip().upper()
    
    # Debug log
    logger.info(f"Role code verification attempt. Selected role: '{selected_role}', Code entered: '{code}'")
    logger.info(f"Available codes - HOST: '{host_code}', MOD: '{mod_code}', CO_OWNER: '{co_owner_code}', OWNER: '{owner_code}'")
    
    # Validate the selected role and code match
    role = None
    code_valid = False
    
    if selected_role == 'host' and code == host_code:
        role = 'host'
        code_valid = True
    elif selected_role == 'mod' and code == mod_code:
        role = 'mod'
        code_valid = True
    elif selected_role == 'co_owner' and code == co_owner_code:
        role = 'co_owner'
        code_valid = True
    elif selected_role == 'owner' and code == owner_code:
        role = 'owner'
        code_valid = True
    
    if not code_valid:
        return jsonify({"success": False, "error": f"Invalid code for {selected_role.replace('_', ' ').title()} role."})
    
    if role:
        conn, db_type = get_db()
        c = get_cursor(conn, db_type)
        
        try:
            is_admin = 1 if role in ['owner', 'co_owner', 'mod', 'host'] else 0
            
            if db_type == 'postgres':
                c.execute("UPDATE users SET is_admin = %s, role = %s WHERE id = %s", (is_admin, role, session['user_id']))
            else:
                c.execute("UPDATE users SET is_admin = ?, role = ? WHERE id = ?", (is_admin, role, session['user_id']))
            
            conn.commit()
            
            session['is_admin'] = bool(is_admin)
            session['role'] = role
            if role in ['owner', 'co_owner', 'mod', 'host']:
                session['admin_role'] = role
            log_action(session['user_id'], 'role_assigned', details={'role': role, 'code_used': True})
            
            logger.info(f"Role assigned successfully: {role} for user {session['user_id']}")
            
            role_display = role.replace('_', ' ').title()
            return jsonify({"success": True, "role": role, "role_display": role_display})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
        finally:
            conn.close()

# ========== SHOP & XP SYSTEM ==========

DEFAULT_SHOP_ITEMS = [
    # ==================== AVATAR FRAMES ====================
    # Common Frames (200-500 XP)
    {"item_id": "frame_wood", "name": "Wooden Frame", "description": "A simple wooden frame", "category": "frame", "price": 200, "rarity": "common", "icon": "🪵", "effects": {"frame_color": "#8B4513", "glow": False}},
    {"item_id": "frame_stone", "name": "Stone Ring", "description": "Solid as a rock", "category": "frame", "price": 350, "rarity": "common", "icon": "🪨", "effects": {"frame_color": "#808080", "glow": False}},
    
    # Rare Frames (1k-3k XP)
    {"item_id": "frame_bronze", "name": "Bronze Ring", "description": "A warm bronze frame", "category": "frame", "price": 1000, "rarity": "rare", "icon": "🥉", "effects": {"frame_color": "#CD7F32", "glow": False}},
    {"item_id": "frame_heart", "name": "Love Heart", "description": "A loving heart frame", "category": "frame", "price": 1500, "rarity": "rare", "icon": "💖", "effects": {"frame_style": "heart", "animation": "pulse"}},
    {"item_id": "frame_cross", "name": "Holy Cross", "description": "A blessed cross frame", "category": "frame", "price": 2500, "rarity": "rare", "icon": "✝️", "effects": {"frame_style": "cross", "glow": True}},
    
    # Epic Frames (5k-8k XP)
    {"item_id": "frame_silver", "name": "Silver Crown", "description": "An elegant silver frame", "category": "frame", "price": 5000, "rarity": "epic", "icon": "🥈", "effects": {"frame_color": "#C0C0C0", "glow": True}},
    {"item_id": "frame_nature", "name": "Nature's Embrace", "description": "Wrapped in leaves and vines", "category": "frame", "price": 6000, "rarity": "epic", "icon": "🌿", "effects": {"frame_style": "nature", "animation": "sway"}},
    {"item_id": "frame_ice", "name": "Frost Edge", "description": "Cool and crystalline", "category": "frame", "price": 7500, "rarity": "epic", "icon": "❄️", "effects": {"frame_color": "#00CED1", "glow": True}},
    {"item_id": "frame_fire", "name": "Flame Border", "description": "Burning with passion", "category": "frame", "price": 8000, "rarity": "epic", "icon": "🔥", "effects": {"frame_style": "fire", "animation": "flicker"}},
    
    # Legendary Frames (12k-20k XP)
    {"item_id": "frame_gold", "name": "Golden Halo", "description": "A radiant golden frame for your avatar", "category": "frame", "price": 12000, "rarity": "legendary", "icon": "👑", "effects": {"frame_color": "#FFD700", "glow": True}},
    {"item_id": "frame_stars", "name": "Starry Night", "description": "Sparkling with cosmic energy", "category": "frame", "price": 18000, "rarity": "legendary", "icon": "✨", "effects": {"frame_style": "stars", "animation": "twinkle"}},
    {"item_id": "frame_angel", "name": "Angel Wings", "description": "Beautiful angel wings frame", "category": "frame", "price": 20000, "rarity": "legendary", "icon": "🪽", "effects": {"frame_style": "wings", "animation": "float"}},
    
    # Mythic Frames (50k-100k XP) - ULTRA RARE
    {"item_id": "frame_divine", "name": "Divine Radiance", "description": "Blessed by the heavens themselves", "category": "frame", "price": 50000, "rarity": "mythic", "icon": "😇", "effects": {"frame_style": "divine", "animation": "holy_glow", "particles": True}},
    {"item_id": "frame_cosmic", "name": "Cosmic Entity", "description": "Power from beyond the stars", "category": "frame", "price": 75000, "rarity": "mythic", "icon": "🌌", "effects": {"frame_style": "cosmic", "animation": "galaxy_spin", "particles": True}},
    {"item_id": "frame_infinity", "name": "Infinity Loop", "description": "Eternal and unbreakable", "category": "frame", "price": 100000, "rarity": "mythic", "icon": "♾️", "effects": {"frame_style": "infinity", "animation": "eternal", "glow": True}},
    
    # Transcendent Frames (180k+ XP) - ABOVE MYTHIC
    {"item_id": "frame_seraphic", "name": "Seraphic Crown", "description": "An ascended halo beyond mythic", "category": "frame", "price": 180000, "rarity": "transcendent", "icon": "👼", "effects": {"frame_style": "seraphic", "animation": "prismatic_aura", "particles": True}},
    
    # ==================== NAME COLORS ====================
    # Common Colors (100-500 XP)
    {"item_id": "color_blue", "name": "Ocean Blue", "description": "Deep sea blue name", "category": "name_color", "price": 100, "rarity": "common", "icon": "🔵", "effects": {"color": "#0A84FF", "glow": False}},
    {"item_id": "color_red", "name": "Ruby Red", "description": "Passionate red name", "category": "name_color", "price": 100, "rarity": "common", "icon": "🔴", "effects": {"color": "#FF375F", "glow": False}},
    {"item_id": "color_pink", "name": "Pretty Pink", "description": "Sweet pink name", "category": "name_color", "price": 250, "rarity": "common", "icon": "🩷", "effects": {"color": "#FF69B4", "glow": False}},
    {"item_id": "color_orange", "name": "Sunset Orange", "description": "Warm like the sunset", "category": "name_color", "price": 400, "rarity": "common", "icon": "🟠", "effects": {"color": "#FF9500", "glow": False}},
    {"item_id": "color_teal", "name": "Tropical Teal", "description": "Refreshing tropical color", "category": "name_color", "price": 500, "rarity": "common", "icon": "🩵", "effects": {"color": "#00CED1", "glow": False}},
    
    # Rare Colors (1.5k-3k XP)
    {"item_id": "color_purple", "name": "Royal Purple", "description": "Majestic purple name", "category": "name_color", "price": 1500, "rarity": "rare", "icon": "🟣", "effects": {"color": "#BF5AF2", "glow": True}},
    {"item_id": "color_green", "name": "Emerald Green", "description": "Rich emerald name", "category": "name_color", "price": 2000, "rarity": "rare", "icon": "🟢", "effects": {"color": "#30D158", "glow": True}},
    {"item_id": "color_cyan", "name": "Cyber Cyan", "description": "Digital world cyan", "category": "name_color", "price": 2800, "rarity": "rare", "icon": "💎", "effects": {"color": "#00FFFF", "glow": True}},
    
    # Epic Colors (5k-8k XP)
    {"item_id": "color_neon", "name": "Neon Glow", "description": "Electric neon effect", "category": "name_color", "price": 5000, "rarity": "epic", "icon": "⚡", "effects": {"color": "#39FF14", "glow": True, "animation": "pulse"}},
    {"item_id": "color_plasma", "name": "Plasma Pink", "description": "Glowing plasma energy", "category": "name_color", "price": 6500, "rarity": "epic", "icon": "🌸", "effects": {"color": "#FF00FF", "glow": True, "animation": "pulse"}},
    {"item_id": "color_gold", "name": "Golden Name", "description": "Shine with golden text", "category": "name_color", "price": 8000, "rarity": "epic", "icon": "🌟", "effects": {"color": "#FFD700", "glow": True}},
    
    # Legendary Colors (12k-20k XP)
    {"item_id": "color_rainbow", "name": "Rainbow Name", "description": "Cycle through all colors", "category": "name_color", "price": 15000, "rarity": "legendary", "icon": "🌈", "effects": {"gradient": True, "colors": ["#FF0000", "#FF7F00", "#FFFF00", "#00FF00", "#0000FF", "#4B0082", "#9400D3"]}},
    {"item_id": "color_aurora", "name": "Aurora Borealis", "description": "Dancing northern lights", "category": "name_color", "price": 18000, "rarity": "legendary", "icon": "🎆", "effects": {"gradient": True, "colors": ["#00FF87", "#60EFFF", "#0061FF"], "animation": "shimmer"}},
    
    # Mythic Colors (50k-100k XP) - ULTRA RARE
    {"item_id": "color_phoenix", "name": "Phoenix Fire", "description": "Rise from the ashes", "category": "name_color", "price": 50000, "rarity": "mythic", "icon": "🔥", "effects": {"gradient": True, "colors": ["#FF0000", "#FF6600", "#FFCC00"], "animation": "flame"}},
    {"item_id": "color_void", "name": "Void Walker", "description": "Darkness beyond comprehension", "category": "name_color", "price": 75000, "rarity": "mythic", "icon": "🌑", "effects": {"color": "#1a0033", "glow": True, "shadow": True, "animation": "void_pulse"}},
    {"item_id": "color_godly", "name": "Godly Aura", "description": "Divine presence itself", "category": "name_color", "price": 190000, "rarity": "transcendent", "icon": "👑", "effects": {"gradient": True, "colors": ["#FFD700", "#FFFFFF", "#FFD700"], "animation": "divine_radiance"}},
    
    # Transcendent Colors
    {"item_id": "color_celestial", "name": "Celestial Prism", "description": "Starlight forged into your name", "category": "name_color", "price": 170000, "rarity": "transcendent", "icon": "🌠", "effects": {"gradient": True, "colors": ["#FFFFFF", "#8AFFF5", "#C58CFF", "#FFF5B6"], "animation": "celestial_shift"}},
    {"item_id": "color_edenlight", "name": "Edenlight", "description": "Soft living light from Eden", "category": "name_color", "price": 110000, "rarity": "mythic", "icon": "🌿", "effects": {"gradient": True, "colors": ["#D9FFB2", "#7DFFF8"], "animation": "eden_bloom"}},
    
    # ==================== TITLES ====================
    # Common Titles (500-1k XP)
    {"item_id": "title_seeker", "name": "Seeker", "description": "One who searches for truth", "category": "title", "price": 500, "rarity": "common", "icon": "🔍", "effects": {"title": "Seeker", "prefix": True}},
    {"item_id": "title_messenger", "name": "Messenger", "description": "Carrier of the Word", "category": "title", "price": 800, "rarity": "common", "icon": "✉️", "effects": {"title": "Messenger", "prefix": True}},
    {"item_id": "title_disciple", "name": "Disciple", "description": "A devoted follower", "category": "title", "price": 1000, "rarity": "common", "icon": "🙏", "effects": {"title": "Disciple", "prefix": True}},
    {"item_id": "title_servant", "name": "Servant", "description": "Faithful in the small things", "category": "title", "price": 1200, "rarity": "common", "icon": "🕯️", "effects": {"title": "Servant", "prefix": True}},
    
    # Rare Titles (2k-4k XP)
    {"item_id": "title_worshiper", "name": "Worshiper", "description": "Heart of worship", "category": "title", "price": 2000, "rarity": "rare", "icon": "🎵", "effects": {"title": "Worshiper", "prefix": True}},
    {"item_id": "title_scholar", "name": "Bible Scholar", "description": "Show your dedication to study", "category": "title", "price": 3000, "rarity": "rare", "icon": "📖", "effects": {"title": "Bible Scholar", "prefix": True}},
    {"item_id": "title_warrior", "name": "Prayer Warrior", "description": "A warrior in prayer", "category": "title", "price": 4000, "rarity": "rare", "icon": "⚔️", "effects": {"title": "Prayer Warrior", "prefix": True}},
    
    # Epic Titles (8k-15k XP)
    {"item_id": "title_pastor", "name": "Pastor", "description": "Shepherd of the flock", "category": "title", "price": 8000, "rarity": "epic", "icon": "🐑", "effects": {"title": "Pastor", "prefix": True}},
    {"item_id": "title_evangelist", "name": "Evangelist", "description": "Bearer of good news", "category": "title", "price": 12000, "rarity": "epic", "icon": "📢", "effects": {"title": "Evangelist", "prefix": True}},
    {"item_id": "title_reverend", "name": "Reverend", "description": "Worthy of respect", "category": "title", "price": 15000, "rarity": "epic", "icon": "⛪", "effects": {"title": "Reverend", "prefix": True}},
    
    # Legendary Titles (20k-35k XP)
    {"item_id": "title_prophet", "name": "Prophet", "description": "Speaker of truth", "category": "title", "price": 20000, "rarity": "legendary", "icon": "🔮", "effects": {"title": "Prophet", "prefix": True}},
    {"item_id": "title_apostle", "name": "Apostle", "description": "One who is sent forth", "category": "title", "price": 28000, "rarity": "legendary", "icon": "📜", "effects": {"title": "Apostle", "prefix": True}},
    {"item_id": "title_saint", "name": "Saint", "description": "Recognized for righteousness", "category": "title", "price": 35000, "rarity": "legendary", "icon": "😇", "effects": {"title": "Saint", "prefix": True, "glow": True}},
    
    # Mythic Titles (60k-150k XP) - ULTRA RARE
    {"item_id": "title_archangel", "name": "Archangel", "description": "Messenger of the divine", "category": "title", "price": 60000, "rarity": "mythic", "icon": "🗡️", "effects": {"title": "Archangel", "prefix": True, "glow": True}},
    {"item_id": "title_messiah", "name": "Messiah", "description": "The anointed one", "category": "title", "price": 100000, "rarity": "mythic", "icon": "✨", "effects": {"title": "Messiah", "prefix": True, "glow": True}},
    {"item_id": "title_god", "name": "Throne Keeper", "description": "Guardian near the eternal throne", "category": "title", "price": 190000, "rarity": "transcendent", "icon": "🛐", "effects": {"title": "Throne Keeper", "prefix": True, "glow": True}},
    
    # Transcendent Titles
    {"item_id": "title_thronekeeper", "name": "Creator", "description": "Crowned at the highest rank", "category": "title", "price": 260000, "rarity": "transcendent", "icon": "👑", "effects": {"title": "Creator", "prefix": True, "glow": True}},
    {"item_id": "title_alphaomega", "name": "Alpha Omega", "description": "Beginning and end", "category": "title", "price": 300000, "rarity": "transcendent", "icon": "☀️", "effects": {"title": "Alpha Omega", "prefix": True, "glow": True}},
    
    # ==================== BADGES ====================
    # Common Badges (200-800 XP)
    {"item_id": "badge_seed", "name": "Seed Planter", "description": "Just starting to grow", "category": "badge", "price": 200, "rarity": "common", "icon": "🌱", "effects": {"badge": "seed", "color": "#90EE90"}},
    {"item_id": "badge_cross", "name": "Faithful", "description": "Steadfast in faith", "category": "badge", "price": 500, "rarity": "common", "icon": "✝️", "effects": {"badge": "cross", "color": "#8B4513"}},
    {"item_id": "badge_verified", "name": "Verified", "description": "A verified member", "category": "badge", "price": 800, "rarity": "common", "icon": "✓", "effects": {"badge": "verified", "color": "#0A84FF"}},
    
    # Rare Badges (2k-4k XP)
    {"item_id": "badge_heart", "name": "Loved", "description": "Spreading love", "category": "badge", "price": 2000, "rarity": "rare", "icon": "💝", "effects": {"badge": "heart", "color": "#FF375F"}},
    {"item_id": "badge_star", "name": "Star Member", "description": "Shining star of the community", "category": "badge", "price": 3000, "rarity": "rare", "icon": "⭐", "effects": {"badge": "star", "color": "#FFD700"}},
    {"item_id": "badge_prayer", "name": "Prayer Warrior", "description": "Warrior in prayer", "category": "badge", "price": 4000, "rarity": "rare", "icon": "🙏", "effects": {"badge": "prayer", "color": "#BF5AF2"}},
    {"item_id": "badge_anchor", "name": "Anchor", "description": "Steady and unshaken", "category": "badge", "price": 4500, "rarity": "rare", "icon": "⚓", "effects": {"badge": "anchor", "color": "#7AB8FF"}},
    
    # Epic Badges (6k-10k XP)
    {"item_id": "badge_dove", "name": "Peace Dove", "description": "Bringer of peace", "category": "badge", "price": 6000, "rarity": "epic", "icon": "🕊️", "effects": {"badge": "dove", "color": "#FFFFFF"}},
    {"item_id": "badge_bible", "name": "Scripture Master", "description": "Knows the Word", "category": "badge", "price": 8500, "rarity": "epic", "icon": "📖", "effects": {"badge": "bible", "color": "#30D158"}},
    {"item_id": "badge_guardian", "name": "Guardian", "description": "Protector of the faith", "category": "badge", "price": 10000, "rarity": "epic", "icon": "🛡️", "effects": {"badge": "guardian", "color": "#4169E1"}},
    
    # Legendary Badges (15k-25k XP)
    {"item_id": "badge_crown", "name": "Crowned", "description": "Royal recognition", "category": "badge", "price": 15000, "rarity": "legendary", "icon": "👑", "effects": {"badge": "crown", "color": "#FFD700"}},
    {"item_id": "badge_lion", "name": "Lion of Judah", "description": "Strong and courageous", "category": "badge", "price": 22000, "rarity": "legendary", "icon": "🦁", "effects": {"badge": "lion", "color": "#FF8C00"}},
    {"item_id": "badge_trinity", "name": "Holy Trinity", "description": "Father, Son, and Holy Spirit", "category": "badge", "price": 25000, "rarity": "legendary", "icon": "☘️", "effects": {"badge": "trinity", "color": "#00FF7F"}},
    
    # Mythic Badges (40k-80k XP) - ULTRA RARE
    {"item_id": "badge_immortal", "name": "Immortal", "description": "Timeless in faith", "category": "badge", "price": 40000, "rarity": "mythic", "icon": "🔮", "effects": {"badge": "immortal", "color": "#9400D3"}},
    {"item_id": "badge_omniscient", "name": "Omniscient", "description": "All-knowing wisdom", "category": "badge", "price": 60000, "rarity": "mythic", "icon": "👁️", "effects": {"badge": "omniscient", "color": "#FF1493"}},
    {"item_id": "badge_divine", "name": "Divine Being", "description": "Touched by the divine", "category": "badge", "price": 80000, "rarity": "mythic", "icon": "✨", "effects": {"badge": "divine", "color": "#FFD700"}},
    
    # Transcendent Badges
    {"item_id": "badge_omega", "name": "Omega Witness", "description": "Mark of the end and beginning", "category": "badge", "price": 210000, "rarity": "transcendent", "icon": "☄️", "effects": {"badge": "omega", "color": "#7DFFF8"}},
    
    # ==================== CHAT EFFECTS ====================
    # Rare Chat Effects (3k-5k XP)
    {"item_id": "chat_glow", "name": "Glowing Messages", "description": "Your messages glow", "category": "chat_effect", "price": 3500, "rarity": "rare", "icon": "💫", "effects": {"effect": "glow", "color": "#FFD700"}},
    {"item_id": "chat_shadow", "name": "Shadow Text", "description": "Dark mysterious messages", "category": "chat_effect", "price": 4500, "rarity": "rare", "icon": "🌑", "effects": {"effect": "shadow", "color": "#333333"}},
    
    # Epic Chat Effects (8k-12k XP)
    {"item_id": "chat_fire", "name": "Fire Messages", "description": "Burning passion in every message", "category": "chat_effect", "price": 8000, "rarity": "epic", "icon": "🔥", "effects": {"effect": "fire", "animation": "flicker"}},
    {"item_id": "chat_sparkle", "name": "Sparkle Messages", "description": "Your messages sparkle", "category": "chat_effect", "price": 10000, "rarity": "epic", "icon": "✨", "effects": {"effect": "sparkle", "animation": "twinkle"}},
    {"item_id": "chat_ice", "name": "Frozen Messages", "description": "Cool icy text effect", "category": "chat_effect", "price": 12000, "rarity": "epic", "icon": "❄️", "effects": {"effect": "ice", "animation": "freeze"}},
    
    # Legendary Chat Effects (15k-25k XP)
    {"item_id": "chat_rainbow", "name": "Rainbow Text", "description": "Colorful message text", "category": "chat_effect", "price": 18000, "rarity": "legendary", "icon": "🌈", "effects": {"effect": "rainbow", "gradient": True}},
    {"item_id": "chat_gold", "name": "Golden Words", "description": "Every word is precious", "category": "chat_effect", "price": 25000, "rarity": "legendary", "icon": "📜", "effects": {"effect": "gold", "animation": "shimmer"}},
    {"item_id": "chat_halo", "name": "Halo Speech", "description": "Soft holy glow around words", "category": "chat_effect", "price": 32000, "rarity": "legendary", "icon": "💠", "effects": {"effect": "glow", "animation": "halo"}},
    
    # Mythic Chat Effects (50k-100k XP) - ULTRA RARE
    {"item_id": "chat_universe", "name": "Universal Voice", "description": "Echoes across dimensions", "category": "chat_effect", "price": 50000, "rarity": "mythic", "icon": "🌌", "effects": {"effect": "universe", "animation": "cosmic_wave"}},
    {"item_id": "chat_godly", "name": "Godly Speech", "description": "Words of ultimate power", "category": "chat_effect", "price": 200000, "rarity": "transcendent", "icon": "⚡", "effects": {"effect": "godly", "animation": "divine_thunder"}},
    
    # Transcendent Chat Effects
    {"item_id": "chat_revelation", "name": "Revelation Voice", "description": "Speech wrapped in celestial prophecy", "category": "chat_effect", "price": 190000, "rarity": "transcendent", "icon": "📡", "effects": {"effect": "revelation", "animation": "oracle_wave"}},
    {"item_id": "chat_thunder_sigil", "name": "Thunder Sigil", "description": "Electrified transcendent speech", "category": "chat_effect", "price": 280000, "rarity": "transcendent", "icon": "🌩️", "effects": {"effect": "godly", "animation": "storm_sigil"}},
    
    # ==================== PROFILE BACKGROUNDS ====================
    # Rare Backgrounds (3k-5k XP)
    {"item_id": "bg_golden", "name": "Golden Hour", "description": "Warm golden background", "category": "profile_bg", "price": 3000, "rarity": "rare", "icon": "🌅", "effects": {"bg_style": "gradient", "colors": ["#FFD700", "#FFA500"]}},
    {"item_id": "bg_ocean", "name": "Ocean Waves", "description": "Calming ocean vibes", "category": "profile_bg", "price": 4500, "rarity": "rare", "icon": "🌊", "effects": {"bg_style": "waves", "animation": "flow"}},
    {"item_id": "bg_nature", "name": "Garden of Eden", "description": "Lush paradise", "category": "profile_bg", "price": 5000, "rarity": "rare", "icon": "🌳", "effects": {"bg_style": "nature", "colors": ["#228B22", "#90EE90"]}},
    
    # Epic Backgrounds (8k-12k XP)
    {"item_id": "bg_clouds", "name": "Heavenly Clouds", "description": "Walk on clouds", "category": "profile_bg", "price": 8000, "rarity": "epic", "icon": "☁️", "effects": {"bg_style": "clouds", "animation": "float"}},
    {"item_id": "bg_night", "name": "Starry Night", "description": "Beautiful night sky", "category": "profile_bg", "price": 10000, "rarity": "epic", "icon": "🌌", "effects": {"bg_style": "stars", "animation": "twinkle"}},
    {"item_id": "bg_fire", "name": "Holy Fire", "description": "Divine flames", "category": "profile_bg", "price": 12000, "rarity": "epic", "icon": "🔥", "effects": {"bg_style": "fire", "animation": "flicker"}},
    
    # Legendary Backgrounds (18k-30k XP)
    {"item_id": "bg_paradise", "name": "Paradise Lost", "description": "Eden before the fall", "category": "profile_bg", "price": 18000, "rarity": "legendary", "icon": "🏞️", "effects": {"bg_style": "paradise", "colors": ["#00FF87", "#60EFFF"]}},
    {"item_id": "bg_celestial", "name": "Celestial Realm", "description": "Heaven on Earth", "category": "profile_bg", "price": 25000, "rarity": "legendary", "icon": "🏛️", "effects": {"bg_style": "celestial", "animation": "holy_light"}},
    {"item_id": "bg_eternity", "name": "Eternal Void", "description": "Beyond time and space", "category": "profile_bg", "price": 30000, "rarity": "legendary", "icon": "🕳️", "effects": {"bg_style": "void", "animation": "dark_matter"}},
    
    # Mythic Backgrounds (60k-120k XP) - ULTRA RARE
    {"item_id": "bg_divine", "name": "Divine Throne", "description": "Sit at the right hand", "category": "profile_bg", "price": 60000, "rarity": "mythic", "icon": "🪑", "effects": {"bg_style": "divine", "animation": "throne_glow"}},
    {"item_id": "bg_infinity", "name": "Infinite Cosmos", "description": "All of creation", "category": "profile_bg", "price": 100000, "rarity": "mythic", "icon": "♾️", "effects": {"bg_style": "infinity", "animation": "cosmic_dance"}},
    {"item_id": "bg_godly", "name": "Godly Presence", "description": "The presence of the Almighty", "category": "profile_bg", "price": 260000, "rarity": "transcendent", "icon": "👑", "effects": {"bg_style": "gradient", "colors": ["#FBE38A", "#B884FF", "#7DFFF8"], "animation": "omnipotence"}},
    
    # Transcendent Backgrounds
    {"item_id": "bg_new_jerusalem", "name": "New Jerusalem", "description": "A radiant city of eternal light", "category": "profile_bg", "price": 240000, "rarity": "transcendent", "icon": "🌆", "effects": {"bg_style": "gradient", "colors": ["#7DFFF8", "#B884FF", "#FFE38A"], "animation": "heavenfall"}},
    {"item_id": "bg_covenant_light", "name": "Covenant Light", "description": "Soft covenant glow and calm sky", "category": "profile_bg", "price": 140000, "rarity": "mythic", "icon": "🕊️", "effects": {"bg_style": "gradient", "colors": ["#9BE7FF", "#D3B7FF"], "animation": "gentle_shift"}},
    
    # ==================== BOOSTS / CONSUMABLES ====================
    # Epic Boosts
    {"item_id": "ability_double_xp", "name": "Double XP Boost", "description": "2x XP for 24 hours", "category": "consumable", "price": 4000, "rarity": "epic", "icon": "⚡", "effects": {"boost": "double_xp", "duration": "24h", "multiplier": 2}},
    {"item_id": "ability_triple_xp", "name": "Triple XP Boost", "description": "3x XP for 6 hours", "category": "consumable", "price": 8000, "rarity": "legendary", "icon": "🚀", "effects": {"boost": "triple_xp", "duration": "6h", "multiplier": 3}},
    
    # Mythic Boosts
    {"item_id": "ability_quintuple_xp", "name": "Quintuple XP Boost", "description": "5x XP for 1 hour", "category": "consumable", "price": 25000, "rarity": "mythic", "icon": "💫", "effects": {"boost": "quintuple_xp", "duration": "1h", "multiplier": 5}},
    {"item_id": "ability_sevenfold_xp", "name": "Sevenfold XP Boost", "description": "7x XP for 45 minutes", "category": "consumable", "price": 65000, "rarity": "mythic", "icon": "🌀", "effects": {"boost": "sevenfold_xp", "duration": "45m", "multiplier": 7}},
    
    # Transcendent Boosts
    {"item_id": "ability_ascension_xp", "name": "Ascension XP Boost", "description": "10x XP for 30 minutes", "category": "consumable", "price": 120000, "rarity": "transcendent", "icon": "🧬", "effects": {"boost": "ascension_xp", "duration": "30m", "multiplier": 10}},
]

def init_shop_items():
    """Initialize default shop items and update existing ones"""
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        for item in DEFAULT_SHOP_ITEMS:
            effects_json = json.dumps(item['effects']) if db_type == 'postgres' else json.dumps(item['effects'])
            
            if db_type == 'postgres':
                # Insert new items or update existing ones (to sync prices)
                c.execute("""
                    INSERT INTO shop_items (item_id, name, description, category, price, rarity, icon, effects, available)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (item_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        description = EXCLUDED.description,
                        category = EXCLUDED.category,
                        price = EXCLUDED.price,
                        rarity = EXCLUDED.rarity,
                        icon = EXCLUDED.icon,
                        effects = EXCLUDED.effects,
                        available = TRUE
                """, (item['item_id'], item['name'], item['description'], item['category'], 
                      item['price'], item['rarity'], item['icon'], effects_json))
            else:
                # For SQLite, try insert first, then update if exists
                c.execute("""
                    INSERT OR REPLACE INTO shop_items (item_id, name, description, category, price, rarity, icon, effects, available)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """, (item['item_id'], item['name'], item['description'], item['category'],
                      item['price'], item['rarity'], item['icon'], effects_json))
        
        conn.commit()
        logger.info("Shop items initialized/updated")
    except Exception as e:
        logger.error(f"Error initializing shop items: {e}")
    finally:
        conn.close()

# Initialize shop items on startup
init_shop_items()

@app.route('/api/shop/items')
def get_shop_items():
    """Get all available shop items"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    category = request.args.get('category', 'all')
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        if category == 'all':
            if db_type == 'postgres':
                c.execute("""
                    SELECT item_id, name, description, category, price, rarity, icon, effects 
                    FROM shop_items WHERE available = TRUE ORDER BY category, price
                """)
            else:
                c.execute("""
                    SELECT item_id, name, description, category, price, rarity, icon, effects 
                    FROM shop_items WHERE available = 1 ORDER BY category, price
                """)
        else:
            if db_type == 'postgres':
                c.execute("""
                    SELECT item_id, name, description, category, price, rarity, icon, effects 
                    FROM shop_items WHERE category = %s AND available = TRUE ORDER BY price
                """, (category,))
            else:
                c.execute("""
                    SELECT item_id, name, description, category, price, rarity, icon, effects 
                    FROM shop_items WHERE category = ? AND available = 1 ORDER BY price
                """, (category,))
        
        items = []
        for row in c.fetchall():
            effects = row['effects'] if isinstance(row['effects'], dict) else json.loads(row['effects'] or '{}')
            items.append({
                "item_id": row['item_id'] if hasattr(row, 'keys') else row[0],
                "name": row['name'] if hasattr(row, 'keys') else row[1],
                "description": row['description'] if hasattr(row, 'keys') else row[2],
                "category": row['category'] if hasattr(row, 'keys') else row[3],
                "price": row['price'] if hasattr(row, 'keys') else row[4],
                "rarity": row['rarity'] if hasattr(row, 'keys') else row[5],
                "icon": row['icon'] if hasattr(row, 'keys') else row[6],
                "effects": effects
            })
        
        return jsonify({"items": items})
    except Exception as e:
        logger.error(f"Error getting shop items: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/shop/xp')
def get_user_xp():
    """Get user's XP and level"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        # Get or create user XP record
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO user_xp (user_id, xp, total_xp_earned, level)
                VALUES (%s, 0, 0, 1)
                ON CONFLICT (user_id) DO NOTHING
            """, (session['user_id'],))
            c.execute("SELECT xp, total_xp_earned, level FROM user_xp WHERE user_id = %s", (session['user_id'],))
        else:
            c.execute("""
                INSERT OR IGNORE INTO user_xp (user_id, xp, total_xp_earned, level)
                VALUES (?, 0, 0, 1)
            """, (session['user_id'],))
            c.execute("SELECT xp, total_xp_earned, level FROM user_xp WHERE user_id = ?", (session['user_id'],))
        
        row = c.fetchone()
        active_boost = get_active_boost(c, db_type, session['user_id'], cleanup_expired=True)
        conn.commit()
        
        if row:
            xp = row['xp'] if hasattr(row, 'keys') else row[0]
            total_earned = row['total_xp_earned'] if hasattr(row, 'keys') else row[1]
            level = row['level'] if hasattr(row, 'keys') else row[2]
        else:
            xp = total_earned = 0
            level = 1
        
        return jsonify({
            "xp": xp,
            "total_xp_earned": total_earned,
            "level": level,
            "next_level_xp": level * 1000,
            "active_boost": active_boost
        })
    except Exception as e:
        logger.error(f"Error getting user XP: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/shop/purchase', methods=['POST'])
def purchase_item():
    """Purchase an item from the shop"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    data = request.get_json() or {}
    item_id = data.get('item_id')
    
    if not item_id:
        return jsonify({"error": "Item ID required"}), 400
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        # Get item details
        if db_type == 'postgres':
            c.execute("SELECT price, name, category FROM shop_items WHERE item_id = %s AND available = TRUE", (item_id,))
        else:
            c.execute("SELECT price, name, category FROM shop_items WHERE item_id = ? AND available = 1", (item_id,))
        
        item = c.fetchone()
        if not item:
            return jsonify({"error": "Item not found"}), 404
        
        price = item['price'] if hasattr(item, 'keys') else item[0]
        item_name = item['name'] if hasattr(item, 'keys') else item[1]
        category = item['category'] if hasattr(item, 'keys') else item[2]
        
        # Check if user already owns this item (unless it's consumable)
        if category != 'consumable':
            if db_type == 'postgres':
                c.execute("SELECT 1 FROM user_inventory WHERE user_id = %s AND item_id = %s", (session['user_id'], item_id))
            else:
                c.execute("SELECT 1 FROM user_inventory WHERE user_id = ? AND item_id = ?", (session['user_id'], item_id))
            
            if c.fetchone():
                return jsonify({"error": "You already own this item"}), 400
        
        # Get user's XP
        if db_type == 'postgres':
            c.execute("SELECT xp FROM user_xp WHERE user_id = %s", (session['user_id'],))
        else:
            c.execute("SELECT xp FROM user_xp WHERE user_id = ?", (session['user_id'],))
        
        row = c.fetchone()
        current_xp = row['xp'] if (row and hasattr(row, 'keys')) else (row[0] if row else 0)
        
        if current_xp < price:
            return jsonify({"error": "Not enough XP", "needed": price - current_xp}), 400
        
        # Deduct XP
        new_xp = current_xp - price
        owned_quantity = 1
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO user_xp (user_id, xp, total_xp_earned, level)
                VALUES (%s, %s, 0, 1)
                ON CONFLICT (user_id) DO UPDATE SET xp = EXCLUDED.xp
            """, (session['user_id'], new_xp))

            # Add to inventory (consumables can stack).
            if category == 'consumable':
                c.execute("SELECT quantity FROM user_inventory WHERE user_id = %s AND item_id = %s",
                          (session['user_id'], item_id))
                inv_row = c.fetchone()
                if inv_row:
                    existing_qty = int(row_pick(inv_row, 'quantity', 0, 1) or 1)
                    c.execute("""
                        UPDATE user_inventory
                        SET quantity = COALESCE(quantity, 1) + 1, equipped = FALSE
                        WHERE user_id = %s AND item_id = %s
                    """, (session['user_id'], item_id))
                    owned_quantity = existing_qty + 1
                else:
                    c.execute("""
                        INSERT INTO user_inventory (user_id, item_id, equipped, quantity)
                        VALUES (%s, %s, FALSE, 1)
                    """, (session['user_id'], item_id))
                    owned_quantity = 1
            else:
                c.execute("""
                    INSERT INTO user_inventory (user_id, item_id, equipped, quantity)
                    VALUES (%s, %s, FALSE, 1)
                """, (session['user_id'], item_id))
                owned_quantity = 1
            
            # Log transaction
            c.execute("""
                INSERT INTO xp_transactions (user_id, amount, type, description)
                VALUES (%s, %s, 'purchase', %s)
            """, (session['user_id'], -price, f"Purchased {item_name}"))
        else:
            c.execute("""
                INSERT OR REPLACE INTO user_xp (user_id, xp, total_xp_earned, level)
                VALUES (?, ?, COALESCE((SELECT total_xp_earned FROM user_xp WHERE user_id = ?), 0), 
                        COALESCE((SELECT level FROM user_xp WHERE user_id = ?), 1))
            """, (session['user_id'], new_xp, session['user_id'], session['user_id']))

            if category == 'consumable':
                c.execute("SELECT quantity FROM user_inventory WHERE user_id = ? AND item_id = ?",
                          (session['user_id'], item_id))
                inv_row = c.fetchone()
                if inv_row:
                    existing_qty = int(row_pick(inv_row, 'quantity', 0, 1) or 1)
                    c.execute("""
                        UPDATE user_inventory
                        SET quantity = COALESCE(quantity, 1) + 1, equipped = 0
                        WHERE user_id = ? AND item_id = ?
                    """, (session['user_id'], item_id))
                    owned_quantity = existing_qty + 1
                else:
                    c.execute("""
                        INSERT INTO user_inventory (user_id, item_id, equipped, quantity)
                        VALUES (?, ?, 0, 1)
                    """, (session['user_id'], item_id))
                    owned_quantity = 1
            else:
                c.execute("""
                    INSERT INTO user_inventory (user_id, item_id, equipped, quantity)
                    VALUES (?, ?, 0, 1)
                """, (session['user_id'], item_id))
                owned_quantity = 1
            
            c.execute("""
                INSERT INTO xp_transactions (user_id, amount, type, description)
                VALUES (?, ?, 'purchase', ?)
            """, (session['user_id'], -price, f"Purchased {item_name}"))
        
        conn.commit()
        
        return jsonify({
            "success": True,
            "item_id": item_id,
            "name": item_name,
            "remaining_xp": new_xp,
            "category": category,
            "owned_quantity": owned_quantity
        })
    except Exception as e:
        logger.error(f"Error purchasing item: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/shop/inventory')
def get_user_inventory():
    """Get user's purchased items"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        if db_type == 'postgres':
            c.execute("""
                SELECT i.item_id, i.equipped, i.quantity, s.name, s.description, s.category, s.rarity, s.icon, s.effects
                FROM user_inventory i
                JOIN shop_items s ON i.item_id = s.item_id
                WHERE i.user_id = %s
                ORDER BY s.category, s.price
            """, (session['user_id'],))
        else:
            c.execute("""
                SELECT i.item_id, i.equipped, COALESCE(i.quantity, 1), s.name, s.description, s.category, s.rarity, s.icon, s.effects
                FROM user_inventory i
                JOIN shop_items s ON i.item_id = s.item_id
                WHERE i.user_id = ?
                ORDER BY s.category, s.price
            """, (session['user_id'],))
        
        items = []
        for row in c.fetchall():
            effects = row['effects'] if isinstance(row['effects'], dict) else json.loads(row['effects'] or '{}')
            items.append({
                "item_id": row['item_id'] if hasattr(row, 'keys') else row[0],
                "equipped": bool(row['equipped'] if hasattr(row, 'keys') else row[1]),
                "quantity": int(row_pick(row, 'quantity', 2, 1) or 1),
                "name": row['name'] if hasattr(row, 'keys') else row[3],
                "description": row['description'] if hasattr(row, 'keys') else row[4],
                "category": row['category'] if hasattr(row, 'keys') else row[5],
                "rarity": row['rarity'] if hasattr(row, 'keys') else row[6],
                "icon": row['icon'] if hasattr(row, 'keys') else row[7],
                "effects": effects
            })

        active_boost = get_active_boost(c, db_type, session['user_id'], cleanup_expired=True)
        # Keep active boost visible in inventory even when consumed quantity reaches 0.
        if active_boost and active_boost.get("item_id"):
            boost_item_id = str(active_boost.get("item_id"))
            if not any(str(it.get("item_id")) == boost_item_id for it in items):
                if db_type == 'postgres':
                    c.execute("""
                        SELECT item_id, name, description, category, rarity, icon, effects
                        FROM shop_items
                        WHERE item_id = %s
                        LIMIT 1
                    """, (boost_item_id,))
                else:
                    c.execute("""
                        SELECT item_id, name, description, category, rarity, icon, effects
                        FROM shop_items
                        WHERE item_id = ?
                        LIMIT 1
                    """, (boost_item_id,))
                boost_row = c.fetchone()
                if boost_row:
                    boost_effects = row_pick(boost_row, 'effects', 6, {}) or {}
                    if not isinstance(boost_effects, dict):
                        try:
                            boost_effects = json.loads(boost_effects or '{}')
                        except Exception:
                            boost_effects = {}
                    items.append({
                        "item_id": row_pick(boost_row, 'item_id', 0, boost_item_id),
                        "equipped": False,
                        "quantity": 0,
                        "name": row_pick(boost_row, 'name', 1, 'Active Boost'),
                        "description": row_pick(boost_row, 'description', 2, 'Currently active'),
                        "category": row_pick(boost_row, 'category', 3, 'consumable'),
                        "rarity": row_pick(boost_row, 'rarity', 4, 'transcendent'),
                        "icon": row_pick(boost_row, 'icon', 5, '⚡'),
                        "effects": boost_effects
                    })
        conn.commit()
        return jsonify({"inventory": items, "active_boost": active_boost})
    except Exception as e:
        logger.error(f"Error getting inventory: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/shop/equip', methods=['POST'])
def equip_item():
    """Equip or unequip an item"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    data = request.get_json() or {}
    item_id = data.get('item_id')
    equip = data.get('equip', True)
    
    if not item_id:
        return jsonify({"error": "Item ID required"}), 400
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        # Verify user owns this item
        if db_type == 'postgres':
            c.execute("SELECT category, COALESCE(ui.quantity, 1) AS quantity FROM user_inventory ui JOIN shop_items s ON ui.item_id = s.item_id WHERE ui.user_id = %s AND ui.item_id = %s", 
                      (session['user_id'], item_id))
        else:
            c.execute("SELECT category, COALESCE(ui.quantity, 1) AS quantity FROM user_inventory ui JOIN shop_items s ON ui.item_id = s.item_id WHERE ui.user_id = ? AND ui.item_id = ?", 
                      (session['user_id'], item_id))
        
        row = c.fetchone()
        if not row:
            return jsonify({"error": "Item not in inventory"}), 404
        
        category = row['category'] if hasattr(row, 'keys') else row[0]
        quantity = int(row_pick(row, 'quantity', 1, 1) or 1)
        if quantity <= 0:
            return jsonify({"error": "Item quantity is zero"}), 400
        if category == 'consumable':
            return jsonify({"error": "Consumables must be used, not equipped"}), 400
        
        # If equipping, unequip other items in same category (except badges which can stack)
        if equip and category not in ['badge', 'consumable']:
            if db_type == 'postgres':
                c.execute("""
                    UPDATE user_inventory SET equipped = FALSE
                    WHERE user_id = %s AND item_id IN (
                        SELECT item_id FROM shop_items WHERE category = %s
                    )
                """, (session['user_id'], category))
            else:
                c.execute("""
                    UPDATE user_inventory SET equipped = 0
                    WHERE user_id = ? AND item_id IN (
                        SELECT item_id FROM shop_items WHERE category = ?
                    )
                """, (session['user_id'], category))
        
        # Equip/unequip the item
        if db_type == 'postgres':
            c.execute("UPDATE user_inventory SET equipped = %s WHERE user_id = %s AND item_id = %s",
                      (equip, session['user_id'], item_id))
        else:
            c.execute("UPDATE user_inventory SET equipped = ? WHERE user_id = ? AND item_id = ?",
                      (1 if equip else 0, session['user_id'], item_id))
        
        conn.commit()
        
        return jsonify({"success": True, "equipped": equip, "item_id": item_id})
    except Exception as e:
        logger.error(f"Error equipping item: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/shop/use', methods=['POST'])
def use_consumable():
    """Use a consumable booster item."""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json() or {}
    item_id = str(data.get('item_id') or '').strip()
    if not item_id:
        return jsonify({"error": "Item ID required"}), 400

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)

    try:
        ensure_active_boosts_schema(c, db_type)
        # Validate item and ensure it is consumable.
        if db_type == 'postgres':
            c.execute("""
                SELECT category, effects, name
                FROM shop_items
                WHERE item_id = %s AND available = TRUE
            """, (item_id,))
        else:
            c.execute("""
                SELECT category, effects, name
                FROM shop_items
                WHERE item_id = ? AND available = 1
            """, (item_id,))
        item_row = c.fetchone()
        if not item_row:
            return jsonify({"error": "Item not found"}), 404

        category = row_pick(item_row, 'category', 0, '')
        if category != 'consumable':
            return jsonify({"error": "Only consumables can be used"}), 400

        effects_raw = row_pick(item_row, 'effects', 1, {}) or {}
        if isinstance(effects_raw, dict):
            effects = effects_raw
        else:
            try:
                effects = json.loads(effects_raw or '{}')
            except Exception:
                effects = {}

        multiplier = max(1, int(effects.get('multiplier') or 1))
        duration_seconds = parse_duration_to_seconds(effects.get('duration', '1h'), default_seconds=3600)

        # Ensure user owns consumable quantity.
        if db_type == 'postgres':
            c.execute("""
                SELECT COALESCE(quantity, 1) AS quantity
                FROM user_inventory
                WHERE user_id = %s AND item_id = %s
                LIMIT 1
            """, (session['user_id'], item_id))
        else:
            c.execute("""
                SELECT COALESCE(quantity, 1) AS quantity
                FROM user_inventory
                WHERE user_id = ? AND item_id = ?
                LIMIT 1
            """, (session['user_id'], item_id))

        inv_row = c.fetchone()
        current_qty = int(row_pick(inv_row, 'quantity', 0, 0) or 0) if inv_row else 0
        if current_qty <= 0:
            return jsonify({"error": "No boosters owned"}), 400

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=duration_seconds)

        # Consume one quantity.
        remaining_qty = current_qty - 1
        if remaining_qty > 0:
            if db_type == 'postgres':
                c.execute("""
                    UPDATE user_inventory
                    SET quantity = %s, equipped = FALSE
                    WHERE user_id = %s AND item_id = %s
                """, (remaining_qty, session['user_id'], item_id))
            else:
                c.execute("""
                    UPDATE user_inventory
                    SET quantity = ?, equipped = 0
                    WHERE user_id = ? AND item_id = ?
                """, (remaining_qty, session['user_id'], item_id))
        else:
            if db_type == 'postgres':
                c.execute("DELETE FROM user_inventory WHERE user_id = %s AND item_id = %s",
                          (session['user_id'], item_id))
            else:
                c.execute("DELETE FROM user_inventory WHERE user_id = ? AND item_id = ?",
                          (session['user_id'], item_id))

        # Set active boost (single active boost slot per user).
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO user_active_boosts (user_id, item_id, multiplier, started_at, expires_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    item_id = EXCLUDED.item_id,
                    multiplier = EXCLUDED.multiplier,
                    started_at = EXCLUDED.started_at,
                    expires_at = EXCLUDED.expires_at
            """, (session['user_id'], item_id, multiplier, now, expires_at))
        else:
            c.execute("""
                INSERT OR REPLACE INTO user_active_boosts (user_id, item_id, multiplier, started_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
            """, (session['user_id'], item_id, multiplier, now.isoformat(), expires_at.isoformat()))

        if db_type == 'postgres':
            c.execute("""
                INSERT INTO xp_transactions (user_id, amount, type, description)
                VALUES (%s, %s, 'consumable_use', %s)
            """, (session['user_id'], 0, f"Used booster {item_id} ({multiplier}x for {duration_seconds}s)"))
        else:
            c.execute("""
                INSERT INTO xp_transactions (user_id, amount, type, description)
                VALUES (?, ?, 'consumable_use', ?)
            """, (session['user_id'], 0, f"Used booster {item_id} ({multiplier}x for {duration_seconds}s)"))

        active_boost = {
            "item_id": item_id,
            "multiplier": multiplier,
            "started_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "remaining_seconds": duration_seconds
        }

        conn.commit()
        return jsonify({
            "success": True,
            "item_id": item_id,
            "remaining_quantity": remaining_qty,
            "active_boost": active_boost
        })
    except Exception as e:
        logger.error(f"Error using consumable: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/shop/profile/<int:user_id>')
def get_user_profile_customization(user_id):
    """Get a user's equipped profile customizations (public)"""
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        if db_type == 'postgres':
            c.execute("""
                SELECT s.item_id, s.category, s.effects, s.name, s.icon, s.rarity
                FROM user_inventory i
                JOIN shop_items s ON i.item_id = s.item_id
                WHERE i.user_id = %s AND i.equipped = TRUE
            """, (user_id,))
        else:
            c.execute("""
                SELECT s.item_id, s.category, s.effects, s.name, s.icon, s.rarity
                FROM user_inventory i
                JOIN shop_items s ON i.item_id = s.item_id
                WHERE i.user_id = ? AND i.equipped = 1
            """, (user_id,))
        
        customizations = {
            "frame": None,
            "name_color": None,
            "title": None,
            "badges": [],
            "chat_effect": None,
            "profile_bg": None
        }
        
        for row in c.fetchall():
            category = row['category'] if hasattr(row, 'keys') else row[1]
            effects = row['effects'] if isinstance(row['effects'], dict) else json.loads(row['effects'] or '{}')
            item_data = {
                "item_id": row['item_id'] if hasattr(row, 'keys') else row[0],
                "name": row['name'] if hasattr(row, 'keys') else row[3],
                "icon": row['icon'] if hasattr(row, 'keys') else row[4],
                "rarity": row['rarity'] if hasattr(row, 'keys') else row[5],
                "effects": effects
            }
            
            if category == 'badge':
                customizations['badges'].append(item_data)
            elif category in customizations:
                customizations[category] = item_data
        
        return jsonify(customizations)
    except Exception as e:
        logger.error(f"Error getting profile customization: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# ==================== BIBLE LEARNING XP ENDPOINTS ====================

@app.route('/api/bible/verse-read', methods=['POST'])
def track_verse_read():
    """Track when user reads a verse for streak and XP"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    data = request.get_json() or {}
    verse_id = data.get('verse_id')
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        user_id = session['user_id']
        today = datetime.now().date().isoformat()
        
        # Get or create streak record
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO verse_read_streak (user_id, current_streak, longest_streak, last_read_date, total_verses_read)
                VALUES (%s, 1, 1, %s, 1)
                ON CONFLICT (user_id) DO UPDATE SET
                    current_streak = CASE 
                        WHEN verse_read_streak.last_read_date = %s::date - INTERVAL '1 day' THEN verse_read_streak.current_streak + 1
                        WHEN verse_read_streak.last_read_date = %s THEN verse_read_streak.current_streak
                        ELSE 1
                    END,
                    longest_streak = GREATEST(verse_read_streak.longest_streak, 
                        CASE WHEN verse_read_streak.last_read_date != %s THEN 
                            CASE WHEN verse_read_streak.last_read_date = %s::date - INTERVAL '1 day' THEN verse_read_streak.current_streak + 1 ELSE 1 END
                        ELSE verse_read_streak.current_streak END),
                    last_read_date = %s,
                    total_verses_read = verse_read_streak.total_verses_read + 1
                RETURNING current_streak, longest_streak, total_verses_read
            """, (user_id, today, today, today, today, today, today, today))
        else:
            c.execute("""
                INSERT INTO verse_read_streak (user_id, current_streak, longest_streak, last_read_date, total_verses_read)
                VALUES (?, 1, 1, ?, 1)
                ON CONFLICT(user_id) DO UPDATE SET
                    current_streak = CASE 
                        WHEN verse_read_streak.last_read_date = date(?, '-1 day') THEN verse_read_streak.current_streak + 1
                        WHEN verse_read_streak.last_read_date = ? THEN verse_read_streak.current_streak
                        ELSE 1
                    END,
                    longest_streak = MAX(verse_read_streak.longest_streak, 
                        CASE WHEN verse_read_streak.last_read_date != ? THEN 
                            CASE WHEN verse_read_streak.last_read_date = date(?, '-1 day') THEN verse_read_streak.current_streak + 1 ELSE 1 END
                        ELSE verse_read_streak.current_streak END),
                    last_read_date = ?,
                    total_verses_read = verse_read_streak.total_verses_read + 1
            """, (user_id, today, today, today, today, today, today))
            c.execute("SELECT current_streak, longest_streak, total_verses_read FROM verse_read_streak WHERE user_id = ?", (user_id,))
        
        row = c.fetchone()
        current_streak = int(row_pick(row, 'current_streak', 0, 1) or 1)
        longest_streak = int(row_pick(row, 'longest_streak', 1, 1) or 1)
        total_read = int(row_pick(row, 'total_verses_read', 2, 1) or 1)
        
        # Award XP based on streak
        base_xp = 25
        streak_bonus = min(current_streak * 2, 50)  # Max 50 bonus XP
        total_xp = base_xp + streak_bonus
        
        # Award the XP
        award_xp_to_user(user_id, total_xp, f"Read verse (Streak: {current_streak} days)")
        
        conn.commit()
        return jsonify({
            "success": True,
            "xp_earned": total_xp,
            "current_streak": current_streak,
            "longest_streak": longest_streak,
            "total_verses_read": total_read,
            "message": f"📖 +{total_xp} XP! {current_streak} day streak!"
        })
    except Exception as e:
        logger.error(f"Error tracking verse read: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/bible/memorize', methods=['POST'])
def memorize_verse():
    """Mark a verse as memorized and award XP"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    data = request.get_json() or {}
    verse_id = data.get('verse_id')
    
    if not verse_id:
        return jsonify({"error": "verse_id required"}), 400
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        user_id = session['user_id']
        
        # Check if already memorized
        if db_type == 'postgres':
            c.execute("SELECT id FROM verse_memorized WHERE user_id = %s AND verse_id = %s", (user_id, verse_id))
        else:
            c.execute("SELECT id FROM verse_memorized WHERE user_id = ? AND verse_id = ?", (user_id, verse_id))
        
        if c.fetchone():
            return jsonify({"error": "Verse already memorized"}), 400
        
        # Add to memorized
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO verse_memorized (user_id, verse_id, memorized_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
            """, (user_id, verse_id))
        else:
            c.execute("""
                INSERT INTO verse_memorized (user_id, verse_id, memorized_at)
                VALUES (?, ?, datetime('now'))
            """, (user_id, verse_id))
        
        # Award XP for memorization
        award_xp_to_user(user_id, 100, f"Memorized verse #{verse_id}")
        
        # Count total memorized
        if db_type == 'postgres':
            c.execute("SELECT COUNT(*) AS count FROM verse_memorized WHERE user_id = %s", (user_id,))
        else:
            c.execute("SELECT COUNT(*) AS count FROM verse_memorized WHERE user_id = ?", (user_id,))
        
        total_row = c.fetchone()
        total_memorized = int(row_pick(total_row, 'count', 0, 0) or 0)
        
        conn.commit()
        return jsonify({
            "success": True,
            "xp_earned": 100,
            "total_memorized": total_memorized,
            "message": f"🧠 +100 XP! Verse memorized! ({total_memorized} total)"
        })
    except Exception as e:
        logger.error(f"Error memorizing verse: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/bible/study-note', methods=['POST'])
def add_study_note():
    """Add a Bible study note and earn XP"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    data = request.get_json()
    verse_id = data.get('verse_id')
    book = data.get('book')
    chapter = data.get('chapter')
    note_text = data.get('note_text', '').strip()
    
    if not note_text or len(note_text) < 10:
        return jsonify({"error": "Note must be at least 10 characters"}), 400
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        user_id = session['user_id']
        
        # Add the note
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO bible_study_notes (user_id, verse_id, book, chapter, note_text, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING id
            """, (user_id, verse_id, book, chapter, note_text))
        else:
            c.execute("""
                INSERT INTO bible_study_notes (user_id, verse_id, book, chapter, note_text, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """, (user_id, verse_id, book, chapter, note_text))
        
        # Award XP based on note length
        xp = min(50 + (len(note_text) // 50), 200)  # 50-200 XP based on length
        award_xp_to_user(user_id, xp, "Added Bible study note")
        
        conn.commit()
        return jsonify({
            "success": True,
            "xp_earned": xp,
            "message": f"📝 +{xp} XP! Study note added!"
        })
    except Exception as e:
        logger.error(f"Error adding study note: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/bible/prayer', methods=['POST'])
def add_prayer_journal():
    """Add prayer to journal and earn XP"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    data = request.get_json()
    prayer_title = data.get('title', 'Untitled Prayer')
    prayer_content = data.get('content', '').strip()
    
    if not prayer_content or len(prayer_content) < 20:
        return jsonify({"error": "Prayer must be at least 20 characters"}), 400
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        user_id = session['user_id']
        
        # Add prayer
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO prayer_journal (user_id, prayer_title, prayer_content, created_at)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                RETURNING id
            """, (user_id, prayer_title, prayer_content))
        else:
            c.execute("""
                INSERT INTO prayer_journal (user_id, prayer_title, prayer_content, created_at)
                VALUES (?, ?, ?, datetime('now'))
            """, (user_id, prayer_title, prayer_content))
        
        # Award XP
        award_xp_to_user(user_id, 75, "Added prayer to journal")
        
        conn.commit()
        return jsonify({
            "success": True,
            "xp_earned": 75,
            "message": "🙏 +75 XP! Prayer recorded!"
        })
    except Exception as e:
        logger.error(f"Error adding prayer: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/bible/reading-progress', methods=['GET', 'POST'])
def reading_progress():
    """Get or update Bible reading progress"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    user_id = session['user_id']
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        if request.method == 'POST':
            data = request.get_json()
            book = data.get('book')
            chapter = data.get('chapter')
            completed = data.get('completed', False)
            
            if db_type == 'postgres':
                # Get current progress
                c.execute("SELECT books_completed, total_chapters_read FROM reading_progress WHERE user_id = %s", (user_id,))
                row = c.fetchone()
                
                books_completed = json.loads(row[0]) if row and row[0] else []
                total_chapters = row[1] if row else 0
                
                if completed and book not in books_completed:
                    books_completed.append(book)
                    total_chapters += 1
                    
                    # Award XP for completing a chapter
                    award_xp_to_user(user_id, 25, f"Read {book} chapter {chapter}")
                    
                    # Bonus XP for completing a book
                    if len(books_completed) % 5 == 0:  # Every 5 books
                        award_xp_to_user(user_id, 500, f"Completed {len(books_completed)} books!")
                
                c.execute("""
                    INSERT INTO reading_progress (user_id, current_book, current_chapter, books_completed, total_chapters_read, last_read_at)
                    VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (user_id) DO UPDATE SET
                        current_book = %s,
                        current_chapter = %s,
                        books_completed = %s,
                        total_chapters_read = %s,
                        last_read_at = CURRENT_TIMESTAMP
                """, (user_id, book, chapter, json.dumps(books_completed), total_chapters,
                      book, chapter, json.dumps(books_completed), total_chapters))
            else:
                c.execute("SELECT books_completed, total_chapters_read FROM reading_progress WHERE user_id = ?", (user_id,))
                row = c.fetchone()
                
                books_completed = json.loads(row[0]) if row and row[0] else []
                total_chapters = row[1] if row else 0
                
                if completed and book not in books_completed:
                    books_completed.append(book)
                    total_chapters += 1
                    award_xp_to_user(user_id, 25, f"Read {book} chapter {chapter}")
                    if len(books_completed) % 5 == 0:
                        award_xp_to_user(user_id, 500, f"Completed {len(books_completed)} books!")
                
                c.execute("""
                    INSERT OR REPLACE INTO reading_progress 
                    (user_id, current_book, current_chapter, books_completed, total_chapters_read, last_read_at)
                    VALUES (?, ?, ?, ?, ?, datetime('now'))
                """, (user_id, book, chapter, json.dumps(books_completed), total_chapters))
            
            conn.commit()
            return jsonify({
                "success": True,
                "books_completed": len(books_completed),
                "total_chapters": total_chapters,
                "message": f"📚 Progress updated! {len(books_completed)} books completed!"
            })
        
        else:  # GET
            if db_type == 'postgres':
                c.execute("SELECT * FROM reading_progress WHERE user_id = %s", (user_id,))
            else:
                c.execute("SELECT * FROM reading_progress WHERE user_id = ?", (user_id,))
            
            row = c.fetchone()
            if row:
                return jsonify({
                    "current_book": row[1] if hasattr(row, '__iter__') else row['current_book'],
                    "current_chapter": row[2] if hasattr(row, '__iter__') else row['current_chapter'],
                    "total_chapters_read": row[3] if hasattr(row, '__iter__') else row['total_chapters_read'],
                    "books_completed": json.loads(row[4]) if hasattr(row, '__iter__') else json.loads(row['books_completed'])
                })
            return jsonify({"current_book": "Genesis", "current_chapter": 1, "total_chapters_read": 0, "books_completed": []})
    except Exception as e:
        logger.error(f"Error with reading progress: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/bible/trivia', methods=['POST'])
def submit_trivia_answer():
    """Submit Bible trivia answer and earn XP"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    data = request.get_json()
    category = data.get('category', 'general')
    is_correct = data.get('is_correct', False)
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        user_id = session['user_id']
        
        # Get current stats
        if db_type == 'postgres':
            c.execute("""
                SELECT questions_answered, correct_answers, best_streak FROM bible_trivia_scores
                WHERE user_id = %s AND category = %s
            """, (user_id, category))
        else:
            c.execute("""
                SELECT questions_answered, correct_answers, best_streak FROM bible_trivia_scores
                WHERE user_id = ? AND category = ?
            """, (user_id, category))
        
        row = c.fetchone()
        total_answered = (row[0] if row else 0) + 1
        total_correct = (row[1] if row else 0) + (1 if is_correct else 0)
        
        # Calculate current streak (simplified - in real app track consecutive correct)
        current_streak = (row[2] if row else 0)
        if is_correct:
            current_streak += 1
        else:
            current_streak = 0
        
        best_streak = max(current_streak, row[2] if row else 0)
        
        # Update stats
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO bible_trivia_scores (user_id, category, questions_answered, correct_answers, best_streak, last_played)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id, category) DO UPDATE SET
                    questions_answered = %s,
                    correct_answers = %s,
                    best_streak = GREATEST(bible_trivia_scores.best_streak, %s),
                    last_played = CURRENT_TIMESTAMP
            """, (user_id, category, total_answered, total_correct, best_streak,
                  total_answered, total_correct, best_streak))
        else:
            c.execute("""
                INSERT OR REPLACE INTO bible_trivia_scores 
                (user_id, category, questions_answered, correct_answers, best_streak, last_played)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
            """, (user_id, category, total_answered, total_correct, best_streak))
        
        # Award XP
        if is_correct:
            base_xp = 20
            streak_bonus = min(current_streak * 3, 30)
            total_xp = base_xp + streak_bonus
            award_xp_to_user(user_id, total_xp, f"Trivia correct (streak: {current_streak})")
            message = f"✅ +{total_xp} XP! Correct! Streak: {current_streak}"
        else:
            message = "❌ Not quite! Try again!"
            total_xp = 0
        
        conn.commit()
        return jsonify({
            "success": True,
            "is_correct": is_correct,
            "xp_earned": total_xp,
            "current_streak": current_streak,
            "total_correct": total_correct,
            "message": message
        })
    except Exception as e:
        logger.error(f"Error with trivia: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# Bible Trivia Questions
BIBLE_TRIVIA_QUESTIONS = [
    {"id": 1, "question": "Who was the first man created?", "options": ["Adam", "Noah", "Moses", "Abraham"], "answer": 0, "category": "creation"},
    {"id": 2, "question": "How many days did God take to create the world?", "options": ["3 days", "6 days", "7 days", "40 days"], "answer": 1, "category": "creation"},
    {"id": 3, "question": "Who built the ark?", "options": ["Moses", "Noah", "Abraham", "David"], "answer": 1, "category": "old_testament"},
    {"id": 4, "question": "How many disciples did Jesus have?", "options": ["10", "11", "12", "13"], "answer": 2, "category": "gospels"},
    {"id": 5, "question": "Who betrayed Jesus?", "options": ["Peter", "Judas", "Thomas", "John"], "answer": 1, "category": "gospels"},
    {"id": 6, "question": "What is the first book of the Bible?", "options": ["Exodus", "Genesis", "Matthew", "Psalms"], "answer": 1, "category": "bible_basics"},
    {"id": 7, "question": "Who led the Israelites out of Egypt?", "options": ["Abraham", "Joseph", "Moses", "David"], "answer": 2, "category": "old_testament"},
    {"id": 8, "question": "What did Jesus turn water into?", "options": ["Oil", "Blood", "Wine", "Milk"], "answer": 2, "category": "miracles"},
    {"id": 9, "question": "How many books are in the Bible?", "options": ["66", "72", "39", "27"], "answer": 0, "category": "bible_basics"},
    {"id": 10, "question": "Who was swallowed by a great fish?", "options": ["Jonah", "Peter", "Paul", "John"], "answer": 0, "category": "old_testament"},
    {"id": 11, "question": "What is the Golden Rule?", "options": [
        "Love your neighbor",
        "Do to others as you would have them do to you",
        "Honor your parents",
        "Pray without ceasing"
    ], "answer": 1, "category": "teachings"},
    {"id": 12, "question": "Who was the strongest man in the Bible?", "options": ["Goliath", "Samson", "David", "Solomon"], "answer": 1, "category": "old_testament"},
    {"id": 13, "question": "What is the last book of the Bible?", "options": ["Jude", "Revelation", "John", "Acts"], "answer": 1, "category": "bible_basics"},
    {"id": 14, "question": "Who wrote most of the Psalms?", "options": ["Solomon", "David", "Moses", "Asaph"], "answer": 1, "category": "wisdom"},
    {"id": 15, "question": "What was Paul's name before conversion?", "options": ["Simon", "Saul", "Stephen", "Silas"], "answer": 1, "category": "acts"},
    {"id": 16, "question": "How many commandments did God give Moses?", "options": ["5", "10", "12", "7"], "answer": 1, "category": "law"},
    {"id": 17, "question": "Who was the first murderer in the Bible?", "options": ["Lamech", "Cain", "Seth", "Abel"], "answer": 1, "category": "old_testament"},
    {"id": 18, "question": "What did Jesus feed the 5,000 with?", "options": ["Fish and bread", "Manna", "Wine", "Locusts"], "answer": 0, "category": "miracles"},
    {"id": 19, "question": "Who was the mother of Jesus?", "options": ["Martha", "Mary Magdalene", "Mary", "Elizabeth"], "answer": 2, "category": "gospels"},
    {"id": 20, "question": "What is the shortest verse in the Bible?", "options": [
        "Jesus wept",
        "God is love",
        "Pray always",
        "Love never fails"
    ], "answer": 0, "category": "bible_basics"}
]

@app.route('/api/bible/trivia-questions', methods=['GET'])
def get_trivia_questions():
    """Get Bible trivia questions"""
    category = request.args.get('category', 'all')
    limit = min(int(request.args.get('limit', 5)), 10)
    
    questions = BIBLE_TRIVIA_QUESTIONS
    if category != 'all':
        questions = [q for q in questions if q['category'] == category]
    
    import random
    selected = random.sample(questions, min(limit, len(questions)))
    
    # Remove answer from response
    return jsonify([{
        "id": q["id"],
        "question": q["question"],
        "options": q["options"],
        "category": q["category"]
    } for q in selected])

@app.route('/api/bible/verify-answer', methods=['POST'])
def verify_trivia_answer():
    """Verify a trivia answer"""
    data = request.get_json()
    question_id = data.get('question_id')
    selected_answer = data.get('answer')
    
    question = next((q for q in BIBLE_TRIVIA_QUESTIONS if q["id"] == question_id), None)
    if not question:
        return jsonify({"error": "Question not found"}), 404
    
    is_correct = question["answer"] == selected_answer
    
    return jsonify({
        "is_correct": is_correct,
        "correct_answer": question["answer"],
        "explanation": f"The correct answer is: {question['options'][question['answer']]}"
    })

@app.route('/api/bible/topic-study', methods=['POST'])
def track_topic_study():
    """Track progress on studying a specific Bible topic"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    data = request.get_json()
    topic = data.get('topic')
    verses_studied = data.get('verses_studied', 1)
    study_time = data.get('study_time_minutes', 0)
    completed = data.get('completed', False)
    
    if not topic:
        return jsonify({"error": "topic required"}), 400
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        user_id = session['user_id']
        
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO topic_study_progress (user_id, topic, verses_studied, study_time_minutes, completed, started_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id, topic) DO UPDATE SET
                    verses_studied = topic_study_progress.verses_studied + %s,
                    study_time_minutes = topic_study_progress.study_time_minutes + %s,
                    completed = %s OR topic_study_progress.completed,
                    completed_at = CASE WHEN %s AND NOT topic_study_progress.completed THEN CURRENT_TIMESTAMP 
                                      ELSE topic_study_progress.completed_at END
                RETURNING verses_studied, study_time_minutes, completed
            """, (user_id, topic, verses_studied, study_time, completed,
                  verses_studied, study_time, completed, completed))
        else:
            c.execute("""
                INSERT INTO topic_study_progress (user_id, topic, verses_studied, study_time_minutes, completed, started_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(user_id, topic) DO UPDATE SET
                    verses_studied = topic_study_progress.verses_studied + ?,
                    study_time_minutes = topic_study_progress.study_time_minutes + ?,
                    completed = ? OR topic_study_progress.completed
            """, (user_id, topic, verses_studied, study_time, completed, verses_studied, study_time, completed))
            c.execute("SELECT verses_studied, study_time_minutes, completed FROM topic_study_progress WHERE user_id = ? AND topic = ?", 
                     (user_id, topic))
        
        row = c.fetchone()
        total_verses = row[0] if row else verses_studied
        total_time = row[1] if row else study_time
        is_completed = row[2] if row else completed
        
        # Award XP
        xp = verses_studied * 15 + study_time * 2  # 15 XP per verse, 2 XP per minute
        if completed and not is_completed:
            xp += 200  # Bonus for completing topic study
        
        award_xp_to_user(user_id, xp, f"Studied topic: {topic}")
        
        conn.commit()
        return jsonify({
            "success": True,
            "xp_earned": xp,
            "total_verses_studied": total_verses,
            "total_study_time": total_time,
            "topic_completed": is_completed,
            "message": f"📚 +{xp} XP! Studied {topic}!"
        })
    except Exception as e:
        logger.error(f"Error tracking topic study: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/bible/learning-stats', methods=['GET'])
def get_learning_stats():
    """Get user's Bible learning statistics"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    user_id = session['user_id']
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        stats = {
            "reading_streak": {"current": 0, "longest": 0, "total_verses": 0},
            "memorized": 0,
            "study_notes": 0,
            "prayers": 0,
            "reading_progress": {"books_completed": [], "total_chapters": 0},
            "trivia": {"total_answered": 0, "correct": 0},
            "topic_studies": []
        }
        
        # Reading streak
        if db_type == 'postgres':
            c.execute("SELECT current_streak, longest_streak, total_verses_read FROM verse_read_streak WHERE user_id = %s", (user_id,))
        else:
            c.execute("SELECT current_streak, longest_streak, total_verses_read FROM verse_read_streak WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if row:
            stats["reading_streak"] = {"current": row[0], "longest": row[1], "total_verses": row[2]}
        
        # Memorized verses
        if db_type == 'postgres':
            c.execute("SELECT COUNT(*) FROM verse_memorized WHERE user_id = %s", (user_id,))
        else:
            c.execute("SELECT COUNT(*) FROM verse_memorized WHERE user_id = ?", (user_id,))
        stats["memorized"] = c.fetchone()[0]
        
        # Study notes
        if db_type == 'postgres':
            c.execute("SELECT COUNT(*) FROM bible_study_notes WHERE user_id = %s", (user_id,))
        else:
            c.execute("SELECT COUNT(*) FROM bible_study_notes WHERE user_id = ?", (user_id,))
        stats["study_notes"] = c.fetchone()[0]
        
        # Prayers
        if db_type == 'postgres':
            c.execute("SELECT COUNT(*) FROM prayer_journal WHERE user_id = %s", (user_id,))
        else:
            c.execute("SELECT COUNT(*) FROM prayer_journal WHERE user_id = ?", (user_id,))
        stats["prayers"] = c.fetchone()[0]
        
        # Reading progress
        if db_type == 'postgres':
            c.execute("SELECT books_completed, total_chapters_read FROM reading_progress WHERE user_id = %s", (user_id,))
        else:
            c.execute("SELECT books_completed, total_chapters_read FROM reading_progress WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if row:
            stats["reading_progress"] = {
                "books_completed": json.loads(row[0]) if row[0] else [],
                "total_chapters": row[1]
            }
        
        # Trivia totals
        if db_type == 'postgres':
            c.execute("""
                SELECT SUM(questions_answered), SUM(correct_answers) 
                FROM bible_trivia_scores WHERE user_id = %s
            """, (user_id,))
        else:
            c.execute("""
                SELECT SUM(questions_answered), SUM(correct_answers) 
                FROM bible_trivia_scores WHERE user_id = ?
            """, (user_id,))
        row = c.fetchone()
        if row:
            stats["trivia"] = {"total_answered": row[0] or 0, "correct": row[1] or 0}
        
        # Topic studies
        if db_type == 'postgres':
            c.execute("""
                SELECT topic, verses_studied, study_time_minutes, completed 
                FROM topic_study_progress WHERE user_id = %s ORDER BY started_at DESC
            """, (user_id,))
        else:
            c.execute("""
                SELECT topic, verses_studied, study_time_minutes, completed 
                FROM topic_study_progress WHERE user_id = ? ORDER BY started_at DESC
            """, (user_id,))
        stats["topic_studies"] = [
            {"topic": r[0], "verses": r[1], "minutes": r[2], "completed": bool(r[3])}
            for r in c.fetchall()
        ]
        
        return jsonify(stats)
    except Exception as e:
        logger.error(f"Error getting learning stats: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# Helper function to award XP
def award_xp_to_user(user_id, amount, description):
    """Helper to award XP to a user"""
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        # Normalize amount early and no-op invalid/zero awards.
        try:
            amount = int(amount)
        except Exception:
            amount = 0
        if amount <= 0:
            return {
                "success": True,
                "new_total": None,
                "level": None,
                "leveled_up": False,
                "base_amount": 0,
                "awarded_amount": 0,
                "multiplier": 1
            }

        active_boost = get_active_boost(c, db_type, user_id, cleanup_expired=True)
        multiplier = max(1, int((active_boost or {}).get('multiplier', 1) or 1))
        awarded_amount = amount * multiplier

        # Lock/read user XP row to prevent concurrent award race conditions.
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO user_xp (user_id, xp, total_xp_earned, level, updated_at)
                VALUES (%s, 0, 0, 1, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO NOTHING
            """, (user_id,))
            c.execute("SELECT xp, total_xp_earned, level FROM user_xp WHERE user_id = %s FOR UPDATE", (user_id,))
        else:
            try:
                c.execute("BEGIN IMMEDIATE")
            except Exception:
                pass
            c.execute("""
                INSERT OR IGNORE INTO user_xp (user_id, xp, total_xp_earned, level, updated_at)
                VALUES (?, 0, 0, 1, datetime('now'))
            """, (user_id,))
            c.execute("SELECT xp, total_xp_earned, level FROM user_xp WHERE user_id = ?", (user_id,))
        
        row = c.fetchone()
        if row:
            current_xp = int(row_pick(row, 'xp', 0, 0) or 0)
            total_earned = int(row_pick(row, 'total_xp_earned', 1, 0) or 0)
            current_level = int(row_pick(row, 'level', 2, 1) or 1)
        else:
            current_xp = 0
            total_earned = 0
            current_level = 1
        
        new_xp = current_xp + awarded_amount
        new_total = total_earned + awarded_amount
        new_level = (new_total // 1000) + 1
        
        # Update user XP
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO user_xp (user_id, xp, total_xp_earned, level, updated_at)
                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO UPDATE SET
                    xp = %s,
                    total_xp_earned = %s,
                    level = %s,
                    updated_at = CURRENT_TIMESTAMP
            """, (user_id, new_xp, new_total, new_level, new_xp, new_total, new_level))
            
            c.execute("""
                INSERT INTO xp_transactions (user_id, amount, type, description, timestamp)
                VALUES (%s, %s, 'bible_learning', %s, CURRENT_TIMESTAMP)
            """, (user_id, awarded_amount, description))
        else:
            c.execute("""
                INSERT OR REPLACE INTO user_xp (user_id, xp, total_xp_earned, level, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
            """, (user_id, new_xp, new_total, new_level))
            
            c.execute("""
                INSERT INTO xp_transactions (user_id, amount, type, description, timestamp)
                VALUES (?, ?, 'bible_learning', ?, datetime('now'))
            """, (user_id, awarded_amount, description))
        
        conn.commit()
        return {
            "success": True,
            "new_total": new_xp,
            "level": new_level,
            "leveled_up": new_level > current_level,
            "base_amount": amount,
            "awarded_amount": awarded_amount,
            "multiplier": multiplier,
            "active_boost": active_boost
        }
    except Exception as e:
        logger.error(f"Error awarding XP: {e}")
        return {"success": False, "error": str(e)}
    finally:
        conn.close()

@app.route('/api/xp/award', methods=['POST'])
def award_xp():
    """Award XP to user (for actions like likes, saves, comments)"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    data = request.get_json() or {}
    try:
        amount = int(data.get('amount', 0))
    except Exception:
        amount = 0
    action = str(data.get('action', 'unknown') or 'unknown')

    if amount <= 0:
        return jsonify({"error": "Invalid amount"}), 400

    try:
        result = award_xp_to_user(session['user_id'], amount, action)
        if not result.get("success"):
            return jsonify({"error": result.get("error", "Failed to award XP")}), 500
        return jsonify({
            "success": True,
            "xp_awarded": int(result.get("awarded_amount", amount) or amount),
            "base_amount": int(result.get("base_amount", amount) or amount),
            "multiplier": int(result.get("multiplier", 1) or 1),
            "new_total": result.get("new_total"),
            "level": result.get("level"),
            "leveled_up": bool(result.get("leveled_up", False)),
            "active_boost": result.get("active_boost")
        })
    except Exception as e:
        logger.error(f"Error awarding XP: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/stats')
def get_stats():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned"}), 403
    
    conn, db_type = get_db()
    c = conn.cursor()  # Use regular cursor for better compatibility
    
    try:
        ensure_comment_social_tables(c, db_type)
        conn.commit()
        
        # Helper to get count safely
        def safe_count(query, params=None):
            try:
                if params:
                    c.execute(query, params)
                else:
                    c.execute(query)
                row = c.fetchone()
                return row[0] if row else 0
            except Exception as e:
                logger.error(f"Query failed: {query}, error: {e}")
                return 0
        
        if db_type == 'postgres':
            total = safe_count("SELECT COUNT(*) FROM verses")
            liked = safe_count("SELECT COUNT(*) FROM likes WHERE user_id = %s", (session['user_id'],))
            saved = safe_count("SELECT COUNT(*) FROM saves WHERE user_id = %s", (session['user_id'],))
            local_generated = safe_count("SELECT COALESCE(total_verses_read, 0) FROM verse_read_streak WHERE user_id = %s", (session['user_id'],))
            # Count all comments by this user
            comments = safe_count("SELECT COUNT(*) FROM comments WHERE user_id = %s AND COALESCE(is_deleted, 0) = 0", (session['user_id'],))
            # Also count community messages
            community = safe_count("SELECT COUNT(*) FROM community_messages WHERE user_id = %s", (session['user_id'],))
            replies = safe_count("""
                SELECT COUNT(*)
                FROM comment_replies r
                LEFT JOIN comments c
                    ON r.parent_type = 'comment' AND r.parent_id = c.id
                LEFT JOIN community_messages m
                    ON r.parent_type = 'community' AND r.parent_id = m.id
                WHERE r.user_id = %s
                  AND COALESCE(r.is_deleted, 0) = 0
                  AND (
                      (r.parent_type = 'comment' AND COALESCE(c.is_deleted, 0) = 0)
                      OR (r.parent_type = 'community' AND m.id IS NOT NULL)
                  )
            """, (session['user_id'],))
        else:
            total = safe_count("SELECT COUNT(*) FROM verses")
            liked = safe_count("SELECT COUNT(*) FROM likes WHERE user_id = ?", (session['user_id'],))
            saved = safe_count("SELECT COUNT(*) FROM saves WHERE user_id = ?", (session['user_id'],))
            local_generated = safe_count("SELECT COALESCE(total_verses_read, 0) FROM verse_read_streak WHERE user_id = ?", (session['user_id'],))
            comments = safe_count("SELECT COUNT(*) FROM comments WHERE user_id = ? AND COALESCE(is_deleted, 0) = 0", (session['user_id'],))
            community = safe_count("SELECT COUNT(*) FROM community_messages WHERE user_id = ?", (session['user_id'],))
            replies = safe_count("""
                SELECT COUNT(*)
                FROM comment_replies r
                LEFT JOIN comments c
                    ON r.parent_type = 'comment' AND r.parent_id = c.id
                LEFT JOIN community_messages m
                    ON r.parent_type = 'community' AND r.parent_id = m.id
                WHERE r.user_id = ?
                  AND COALESCE(r.is_deleted, 0) = 0
                  AND (
                      (r.parent_type = 'comment' AND COALESCE(c.is_deleted, 0) = 0)
                      OR (r.parent_type = 'community' AND m.id IS NOT NULL)
                  )
            """, (session['user_id'],))
        
        logger.info(f"Stats for user {session['user_id']}: verses={total}, liked={liked}, saved={saved}, comments={comments}, community={community}, replies={replies}")
        
        # Return total comments (verse + community). Replies are separate.
        total_comments = comments + community
        
        return jsonify({
            "total_verses": total,
            "database_verses": total,
            "local_generated_verses": local_generated,
            "liked": liked,
            "saved": saved,
            "comments": total_comments,
            "replies": replies
        })
    except Exception as e:
        logger.error(f"Stats error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "total_verses": 0,
            "database_verses": 0,
            "local_generated_verses": 0,
            "liked": 0,
            "saved": 0,
            "comments": 0,
            "replies": 0
        })
    finally:
        conn.close()

@app.route('/api/profile_stats')
def get_profile_stats():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned"}), 403

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)

    try:
        def safe_count(query, params=None):
            try:
                if params:
                    c.execute(query, params)
                else:
                    c.execute(query)
                row = c.fetchone()
                if not row:
                    return 0
                if isinstance(row, dict) or hasattr(row, 'keys'):
                    values = list(row.values())
                    return int(values[0]) if values else 0
                return int(row[0])
            except Exception:
                return 0

        uid = session['user_id']
        if db_type == 'postgres':
            liked = safe_count("SELECT COUNT(*) FROM likes WHERE user_id = %s", (uid,))
            saved = safe_count("SELECT COUNT(*) FROM saves WHERE user_id = %s", (uid,))
            comments = safe_count("SELECT COUNT(*) FROM comments WHERE user_id = %s AND COALESCE(is_deleted, 0) = 0", (uid,))
            community = safe_count("SELECT COUNT(*) FROM community_messages WHERE user_id = %s", (uid,))
            replies = safe_count("""
                SELECT COUNT(*)
                FROM comment_replies r
                LEFT JOIN comments c ON r.parent_type = 'comment' AND r.parent_id = c.id
                LEFT JOIN community_messages m ON r.parent_type = 'community' AND r.parent_id = m.id
                WHERE r.user_id = %s
                  AND COALESCE(r.is_deleted, 0) = 0
                  AND (
                      (r.parent_type = 'comment' AND COALESCE(c.is_deleted, 0) = 0)
                      OR (r.parent_type = 'community' AND m.id IS NOT NULL)
                  )
            """, (uid,))
            purchases = safe_count("SELECT COALESCE(SUM(COALESCE(quantity, 1)), 0) FROM user_inventory WHERE user_id = %s", (uid,))
            viewed = safe_count("SELECT COALESCE(total_verses_read, 0) FROM verse_read_streak WHERE user_id = %s", (uid,))
            liked_comments = safe_count("""
                SELECT COUNT(*)
                FROM comment_reactions r
                JOIN comments c ON r.item_type = 'comment' AND r.item_id = c.id
                WHERE c.user_id = %s AND LOWER(COALESCE(r.reaction, '')) = 'like' AND r.user_id <> c.user_id
            """, (uid,)) + safe_count("""
                SELECT COUNT(*)
                FROM comment_reactions r
                JOIN community_messages m ON r.item_type = 'community' AND r.item_id = m.id
                WHERE m.user_id = %s AND LOWER(COALESCE(r.reaction, '')) = 'like' AND r.user_id <> m.user_id
            """, (uid,))
        else:
            liked = safe_count("SELECT COUNT(*) FROM likes WHERE user_id = ?", (uid,))
            saved = safe_count("SELECT COUNT(*) FROM saves WHERE user_id = ?", (uid,))
            comments = safe_count("SELECT COUNT(*) FROM comments WHERE user_id = ? AND COALESCE(is_deleted, 0) = 0", (uid,))
            community = safe_count("SELECT COUNT(*) FROM community_messages WHERE user_id = ?", (uid,))
            replies = safe_count("""
                SELECT COUNT(*)
                FROM comment_replies r
                LEFT JOIN comments c ON r.parent_type = 'comment' AND r.parent_id = c.id
                LEFT JOIN community_messages m ON r.parent_type = 'community' AND r.parent_id = m.id
                WHERE r.user_id = ?
                  AND COALESCE(r.is_deleted, 0) = 0
                  AND (
                      (r.parent_type = 'comment' AND COALESCE(c.is_deleted, 0) = 0)
                      OR (r.parent_type = 'community' AND m.id IS NOT NULL)
                  )
            """, (uid,))
            purchases = safe_count("SELECT COALESCE(SUM(COALESCE(quantity, 1)), 0) FROM user_inventory WHERE user_id = ?", (uid,))
            viewed = safe_count("SELECT COALESCE(total_verses_read, 0) FROM verse_read_streak WHERE user_id = ?", (uid,))
            liked_comments = safe_count("""
                SELECT COUNT(*)
                FROM comment_reactions r
                JOIN comments c ON r.item_type = 'comment' AND r.item_id = c.id
                WHERE c.user_id = ? AND LOWER(COALESCE(r.reaction, '')) = 'like' AND r.user_id <> c.user_id
            """, (uid,)) + safe_count("""
                SELECT COUNT(*)
                FROM comment_reactions r
                JOIN community_messages m ON r.item_type = 'community' AND r.item_id = m.id
                WHERE m.user_id = ? AND LOWER(COALESCE(r.reaction, '')) = 'like' AND r.user_id <> m.user_id
            """, (uid,))

        return jsonify({
            "liked": liked,
            "saved": saved,
            "comments": comments + community,
            "replies": replies,
            "total_verses": viewed,
            "purchases": purchases,
            "liked_comments": liked_comments
        })
    except Exception as e:
        logger.error(f"Profile stats error: {e}")
        return jsonify({
            "liked": 0,
            "saved": 0,
            "comments": 0,
            "replies": 0,
            "total_verses": 0,
            "purchases": 0,
            "liked_comments": 0
        })
    finally:
        conn.close()

@app.route('/api/achievements')
def get_achievements():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        ensure_achievement_tables(c, db_type)
        if db_type == 'postgres':
            c.execute("""
                SELECT achievement_id, xp_awarded, unlocked_at
                FROM user_achievements
                WHERE user_id = %s
                ORDER BY unlocked_at ASC
            """, (session['user_id'],))
        else:
            c.execute("""
                SELECT achievement_id, xp_awarded, unlocked_at
                FROM user_achievements
                WHERE user_id = ?
                ORDER BY unlocked_at ASC
            """, (session['user_id'],))

        rows = c.fetchall()
        conn.commit()

        unlocked = []
        total_xp = 0
        for row in rows:
            if isinstance(row, dict) or hasattr(row, 'keys'):
                aid = row['achievement_id']
                xp = int(row['xp_awarded'] or 0)
            else:
                aid = row[0]
                xp = int(row[1] or 0)
            unlocked.append(aid)
            total_xp += xp

        return jsonify({"unlocked": unlocked, "total_xp": total_xp})
    except Exception as e:
        logger.error(f"Get achievements error: {e}")
        return jsonify({"unlocked": [], "total_xp": 0})
    finally:
        conn.close()

@app.route('/api/achievements/unlock', methods=['POST'])
def unlock_achievement():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json() or {}
    achievement_id = str(data.get("achievement_id") or "").strip()
    achievement_name = str(data.get("achievement_name") or achievement_id).strip()
    try:
        xp_amount = int(data.get("xp") or 0)
    except Exception:
        xp_amount = 0
    if not achievement_id:
        return jsonify({"success": False, "error": "achievement_id required"}), 400
    xp_amount = max(0, min(20000, xp_amount))

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        ensure_achievement_tables(c, db_type)
        inserted = False
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO user_achievements (user_id, achievement_id, achievement_name, xp_awarded)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, achievement_id) DO NOTHING
                RETURNING id
            """, (session['user_id'], achievement_id, achievement_name, xp_amount))
            inserted = bool(c.fetchone())
        else:
            c.execute("""
                INSERT OR IGNORE INTO user_achievements (user_id, achievement_id, achievement_name, xp_awarded)
                VALUES (?, ?, ?, ?)
            """, (session['user_id'], achievement_id, achievement_name, xp_amount))
            inserted = bool(c.rowcount and c.rowcount > 0)
        conn.commit()

        if not inserted:
            return jsonify({"success": True, "awarded": False, "achievement_id": achievement_id, "xp_awarded": 0})

        award_result = {"success": True, "new_total": None, "level": None, "leveled_up": False}
        if xp_amount > 0:
            award_result = award_xp_to_user(session['user_id'], xp_amount, f"Achievement unlocked: {achievement_name}")
            if not award_result.get("success"):
                try:
                    if db_type == 'postgres':
                        c.execute("""
                            DELETE FROM user_achievements
                            WHERE user_id = %s AND achievement_id = %s
                        """, (session['user_id'], achievement_id))
                    else:
                        c.execute("""
                            DELETE FROM user_achievements
                            WHERE user_id = ? AND achievement_id = ?
                        """, (session['user_id'], achievement_id))
                    conn.commit()
                except Exception as cleanup_error:
                    logger.error(f"Achievement rollback failed: {cleanup_error}")
                return jsonify({"success": False, "error": award_result.get("error", "Failed to award XP")}), 500

        return jsonify({
            "success": True,
            "awarded": True,
            "achievement_id": achievement_id,
            "xp_awarded": int(award_result.get("awarded_amount", xp_amount) or xp_amount),
            "base_xp": xp_amount,
            "new_total": award_result.get("new_total"),
            "level": award_result.get("level"),
            "leveled_up": award_result.get("leveled_up", False)
        })
    except Exception as e:
        logger.error(f"Unlock achievement error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/daily_challenge')
def get_daily_challenge():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned"}), 403

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    period_key = get_challenge_period_key()
    period_start, period_end = get_hour_window()
    hide_at = period_end
    challenge = pick_hourly_challenge(session['user_id'], period_key)
    goal = challenge.get('goal', 2)
    action = challenge.get('action', 'save')

    try:
        ensure_daily_challenge_tables(c, db_type)
        if db_type == 'postgres':
            c.execute("""
                SELECT COUNT(*) AS count
                FROM daily_actions
                WHERE user_id = %s AND action = %s AND event_date = %s
            """, (session['user_id'], action, period_key))
            row = c.fetchone()
            progress = int(row['count'] if row and isinstance(row, dict) else (row[0] if row else 0))

            c.execute("""
                SELECT 1
                FROM daily_challenge_claims
                WHERE user_id = %s AND challenge_date = %s AND challenge_id = %s
                LIMIT 1
            """, (session['user_id'], period_key, challenge.get('id', 'daily')))
            claimed = bool(c.fetchone())
        else:
            c.execute("""
                SELECT COUNT(*)
                FROM daily_actions
                WHERE user_id = ? AND action = ? AND event_date = ?
            """, (session['user_id'], action, period_key))
            row = c.fetchone()
            progress = int(row[0] if row else 0)

            c.execute("""
                SELECT 1
                FROM daily_challenge_claims
                WHERE user_id = ? AND challenge_date = ? AND challenge_id = ?
                LIMIT 1
            """, (session['user_id'], period_key, challenge.get('id', 'daily')))
            claimed = bool(c.fetchone())

        conn.commit()
        progress = min(progress, goal)
        xp_reward = get_hourly_xp_reward(session['user_id'], period_key, challenge)
        now_ts = datetime.now().astimezone()
        hidden = bool(hide_at and now_ts >= hide_at)
        return jsonify({
            "id": challenge.get('id', 'save2'),
            "text": challenge.get('text', 'Save 2 verses to your library'),
            "goal": goal,
            "type": action,
            "difficulty": challenge.get('difficulty', 'Easy'),
            "date": period_key,
            "challenge_id": challenge.get('id', 'daily'),
            "expires_at": period_end.isoformat(),
            "hide_at": hide_at.isoformat(),
            "hidden": hidden,
            "xp_reward": xp_reward,
            "progress": progress,
            "completed": progress >= goal,
            "claimed": claimed
        })
    except Exception as e:
        logger.error(f"Daily challenge error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/daily_challenge/claim', methods=['POST'])
def claim_daily_challenge():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned"}), 403

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    period_key = get_challenge_period_key()
    challenge = pick_hourly_challenge(session['user_id'], period_key)
    action = challenge.get('action', 'save')
    goal = int(challenge.get('goal', 1))
    challenge_id = challenge.get('id', 'daily')
    xp_reward = get_hourly_xp_reward(session['user_id'], period_key, challenge)

    try:
        ensure_daily_challenge_tables(c, db_type)

        if db_type == 'postgres':
            c.execute("""
                SELECT COUNT(*) AS count
                FROM daily_actions
                WHERE user_id = %s AND action = %s AND event_date = %s
            """, (session['user_id'], action, period_key))
            row = c.fetchone()
            progress = int(row['count'] if row and isinstance(row, dict) else (row[0] if row else 0))
        else:
            c.execute("""
                SELECT COUNT(*)
                FROM daily_actions
                WHERE user_id = ? AND action = ? AND event_date = ?
            """, (session['user_id'], action, period_key))
            row = c.fetchone()
            progress = int(row[0] if row else 0)

        if progress < goal:
            return jsonify({
                "success": False,
                "error": "Challenge not complete",
                "progress": progress,
                "goal": goal
            }), 400

        inserted = False
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO daily_challenge_claims (user_id, challenge_date, challenge_id, xp_awarded)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, challenge_date, challenge_id) DO NOTHING
                RETURNING id
            """, (session['user_id'], period_key, challenge_id, xp_reward))
            inserted = bool(c.fetchone())
        else:
            c.execute("""
                INSERT OR IGNORE INTO daily_challenge_claims (user_id, challenge_date, challenge_id, xp_awarded)
                VALUES (?, ?, ?, ?)
            """, (session['user_id'], period_key, challenge_id, xp_reward))
            inserted = bool(c.rowcount and c.rowcount > 0)

        conn.commit()

        if not inserted:
            return jsonify({
                "success": True,
                "awarded": False,
                "message": "Already claimed",
                "xp_reward": xp_reward
            })

        awarded = award_xp_to_user(session['user_id'], xp_reward, f"Daily challenge ({challenge_id})")
        if not awarded.get("success"):
            try:
                if db_type == 'postgres':
                    c.execute("""
                        DELETE FROM daily_challenge_claims
                        WHERE user_id = %s AND challenge_date = %s AND challenge_id = %s
                    """, (session['user_id'], period_key, challenge_id))
                else:
                    c.execute("""
                        DELETE FROM daily_challenge_claims
                        WHERE user_id = ? AND challenge_date = ? AND challenge_id = ?
                    """, (session['user_id'], period_key, challenge_id))
                conn.commit()
            except Exception as cleanup_error:
                logger.error(f"Daily challenge rollback failed: {cleanup_error}")
            return jsonify({"success": False, "error": awarded.get("error", "XP award failed")}), 500

        return jsonify({
            "success": True,
            "awarded": True,
            "xp_reward": int(awarded.get("awarded_amount", xp_reward) or xp_reward),
            "base_xp_reward": xp_reward,
            "new_total": awarded.get("new_total"),
            "level": awarded.get("level"),
            "leveled_up": awarded.get("leveled_up", False)
        })
    except Exception as e:
        logger.error(f"Daily challenge claim error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/debug/comments')
def debug_comments():
    """Debug endpoint to check comments data"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    conn, db_type = get_db()
    c = conn.cursor()
    
    try:
        # Get total comments count
        c.execute("SELECT COUNT(*) FROM comments")
        total_comments = c.fetchone()[0]
        
        # Get comments by current user
        if db_type == 'postgres':
            c.execute("SELECT COUNT(*) FROM comments WHERE user_id = %s", (session['user_id'],))
        else:
            c.execute("SELECT COUNT(*) FROM comments WHERE user_id = ?", (session['user_id'],))
        user_comments = c.fetchone()[0]
        
        # Get sample comments (last 5)
        if db_type == 'postgres':
            c.execute("SELECT id, user_id, verse_id, text, timestamp FROM comments ORDER BY timestamp DESC LIMIT 5")
        else:
            c.execute("SELECT id, user_id, verse_id, text, timestamp FROM comments ORDER BY timestamp DESC LIMIT 5")
        sample = c.fetchall()
        
        # Get table schema info
        if db_type == 'postgres':
            c.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'comments'")
            columns = [row[0] for row in c.fetchall()]
        else:
            c.execute("PRAGMA table_info(comments)")
            columns = [row[1] for row in c.fetchall()]
        
        return jsonify({
            "total_comments": total_comments,
            "user_comments": user_comments,
            "current_user_id": session['user_id'],
            "sample_comments": [{"id": r[0], "user_id": r[1], "verse_id": r[2], "text": r[3][:50] if r[3] else None, "timestamp": r[4]} for r in sample],
            "table_columns": columns,
            "db_type": db_type
        })
    except Exception as e:
        logger.error(f"Debug error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/like', methods=['POST'])
def like_verse():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned", "message": "Account banned"}), 403
    
    data = request.get_json()
    verse_id = data.get('verse_id')
    verse_payload = data.get('verse') if isinstance(data, dict) else None
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        verse_id = ensure_verse_id(c, db_type, verse_id, verse_payload)
        if db_type == 'postgres':
            c.execute("SELECT id FROM likes WHERE user_id = %s AND verse_id = %s", (session['user_id'], verse_id))
            if c.fetchone():
                c.execute("DELETE FROM likes WHERE user_id = %s AND verse_id = %s", (session['user_id'], verse_id))
                liked = False
            else:
                c.execute("INSERT INTO likes (user_id, verse_id, timestamp) VALUES (%s, %s, %s)",
                          (session['user_id'], verse_id, datetime.now().isoformat()))
                liked = True
        else:
            c.execute("SELECT id FROM likes WHERE user_id = ? AND verse_id = ?", (session['user_id'], verse_id))
            if c.fetchone():
                c.execute("DELETE FROM likes WHERE user_id = ? AND verse_id = ?", (session['user_id'], verse_id))
                liked = False
            else:
                c.execute("INSERT INTO likes (user_id, verse_id, timestamp) VALUES (?, ?, ?)",
                          (session['user_id'], verse_id, datetime.now().isoformat()))
                liked = True
        
        conn.commit()

        if liked:
            record_daily_action(session['user_id'], 'like', verse_id)
            log_user_activity(
                "USER_LIKE",
                user_id=session['user_id'],
                message=f"Liked verse {verse_id}",
                extras={"verse_id": verse_id, "action": "like"}
            )
        else:
            log_user_activity(
                "USER_UNLIKE",
                user_id=session['user_id'],
                message=f"Unliked verse {verse_id}",
                extras={"verse_id": verse_id, "action": "unlike"}
            )
        
        if liked:
            rec = generator.generate_smart_recommendation(session['user_id'])
            return jsonify({"liked": liked, "recommendation": rec})
        
        return jsonify({"liked": liked})
    except Exception as e:
        logger.error(f"Like error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/save', methods=['POST'])
def save_verse():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned"}), 403
    
    data = request.get_json()
    verse_id = data.get('verse_id')
    verse_payload = data.get('verse') if isinstance(data, dict) else None
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        verse_id = ensure_verse_id(c, db_type, verse_id, verse_payload)
        now = datetime.now().isoformat()
        period_key = get_challenge_period_key()
        ensure_daily_challenge_tables(c, db_type)
        if db_type == 'postgres':
            c.execute("SELECT id FROM saves WHERE user_id = %s AND verse_id = %s", (session['user_id'], verse_id))
            if c.fetchone():
                c.execute("DELETE FROM saves WHERE user_id = %s AND verse_id = %s", (session['user_id'], verse_id))
                saved = False
            else:
                c.execute("INSERT INTO saves (user_id, verse_id, timestamp) VALUES (%s, %s, %s)",
                          (session['user_id'], verse_id, now))
                c.execute("""
                    INSERT INTO daily_actions (user_id, action, verse_id, event_date, timestamp)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, action, verse_id, event_date) DO NOTHING
                """, (session['user_id'], 'save', verse_id, period_key, now))
                saved = True
        else:
            c.execute("SELECT id FROM saves WHERE user_id = ? AND verse_id = ?", (session['user_id'], verse_id))
            if c.fetchone():
                c.execute("DELETE FROM saves WHERE user_id = ? AND verse_id = ?", (session['user_id'], verse_id))
                saved = False
            else:
                c.execute("INSERT INTO saves (user_id, verse_id, timestamp) VALUES (?, ?, ?)",
                          (session['user_id'], verse_id, now))
                c.execute("""
                    INSERT OR IGNORE INTO daily_actions (user_id, action, verse_id, event_date, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                """, (session['user_id'], 'save', verse_id, period_key, now))
                saved = True
        
        conn.commit()
        
        # Log the save/unsave action
        if saved:
            log_user_activity(
                "USER_SAVE",
                user_id=session['user_id'],
                message=f"Saved verse {verse_id}",
                extras={"verse_id": verse_id, "action": "save"}
            )
        else:
            log_user_activity(
                "USER_UNSAVE",
                user_id=session['user_id'],
                message=f"Unsaved verse {verse_id}",
                extras={"verse_id": verse_id, "action": "unsave"}
            )
        
        return jsonify({"saved": saved})
    except Exception as e:
        logger.error(f"Save error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/library')
def get_library():
    if 'user_id' not in session:
        return jsonify({"liked": [], "saved": [], "collections": []})
    
    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned"}), 403
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        if db_type == 'postgres':
            c.execute("""
                SELECT v.id, v.reference, v.text, v.translation, v.source, v.book, l.timestamp as liked_at
                FROM verses v 
                JOIN likes l ON v.id = l.verse_id 
                WHERE l.user_id = %s 
                ORDER BY l.timestamp DESC
            """, (session['user_id'],))
            liked = [{"id": row['id'], "ref": row['reference'], "text": row['text'], "trans": row['translation'], 
                      "source": row['source'], "book": row['book'], "liked_at": row['liked_at'], "saved_at": None} for row in c.fetchall()]
            
            c.execute("""
                SELECT v.id, v.reference, v.text, v.translation, v.source, v.book, s.timestamp as saved_at
                FROM verses v 
                JOIN saves s ON v.id = s.verse_id 
                WHERE s.user_id = %s 
                ORDER BY s.timestamp DESC
            """, (session['user_id'],))
            saved = [{"id": row['id'], "ref": row['reference'], "text": row['text'], "trans": row['translation'], 
                      "source": row['source'], "book": row['book'], "liked_at": None, "saved_at": row['saved_at']} for row in c.fetchall()]
            
            # GET COLLECTIONS
            c.execute("""
                SELECT c.id, c.name, c.color, COUNT(vc.verse_id) as count 
                FROM collections c
                LEFT JOIN verse_collections vc ON c.id = vc.collection_id
                WHERE c.user_id = %s
                GROUP BY c.id
            """, (session['user_id'],))
        else:
            c.execute("""
                SELECT v.id, v.reference, v.text, v.translation, v.source, v.book, l.timestamp as liked_at
                FROM verses v 
                JOIN likes l ON v.id = l.verse_id 
                WHERE l.user_id = ? 
                ORDER BY l.timestamp DESC
            """, (session['user_id'],))
            rows = c.fetchall()
            liked = []
            for row in rows:
                try:
                    liked.append({"id": row['id'], "ref": row['reference'], "text": row['text'], "trans": row['translation'], 
                              "source": row['source'], "book": row['book'], "liked_at": row['liked_at'], "saved_at": None})
                except (TypeError, KeyError):
                    liked.append({"id": row[0], "ref": row[1], "text": row[2], "trans": row[3], 
                              "source": row[4], "book": row[6], "liked_at": row[7], "saved_at": None})
            
            c.execute("""
                SELECT v.id, v.reference, v.text, v.translation, v.source, v.book, s.timestamp as saved_at
                FROM verses v 
                JOIN saves s ON v.id = s.verse_id 
                WHERE s.user_id = ? 
                ORDER BY s.timestamp DESC
            """, (session['user_id'],))
            rows = c.fetchall()
            saved = []
            for row in rows:
                try:
                    saved.append({"id": row['id'], "ref": row['reference'], "text": row['text'], "trans": row['translation'], 
                              "source": row['source'], "book": row['book'], "liked_at": None, "saved_at": row['saved_at']})
                except (TypeError, KeyError):
                    saved.append({"id": row[0], "ref": row[1], "text": row[2], "trans": row[3], 
                              "source": row[4], "book": row[6], "liked_at": None, "saved_at": row[7]})
            
            # GET COLLECTIONS
            c.execute("""
                SELECT c.id, c.name, c.color, COUNT(vc.verse_id) as count 
                FROM collections c
                LEFT JOIN verse_collections vc ON c.id = vc.collection_id
                WHERE c.user_id = ?
                GROUP BY c.id
            """, (session['user_id'],))
        
        # Build collections list with verses (bulk-loaded to avoid N+1 queries).
        collection_rows = c.fetchall()
        collections = []
        collection_lookup = {}
        collection_ids = []
        for row in collection_rows:
            try:
                col_id = int(row['id'])
                col_name = row['name']
                col_color = row['color']
                col_count = row['count']
            except (TypeError, KeyError):
                col_id = int(row[0])
                col_name = row[1]
                col_color = row[2]
                col_count = row[3]
            item = {
                "id": col_id,
                "name": col_name,
                "color": col_color,
                "count": col_count,
                "verses": []
            }
            collections.append(item)
            collection_lookup[col_id] = item
            collection_ids.append(col_id)

        ids_sql, ids_params = _build_in_clause_params(db_type, collection_ids)
        if ids_sql:
            if db_type == 'postgres':
                c.execute(f"""
                    SELECT vc.collection_id, v.id, v.reference, v.text
                    FROM verse_collections vc
                    JOIN verses v ON v.id = vc.verse_id
                    WHERE vc.collection_id IN ({ids_sql})
                """, ids_params)
            else:
                c.execute(f"""
                    SELECT vc.collection_id, v.id, v.reference, v.text
                    FROM verse_collections vc
                    JOIN verses v ON v.id = vc.verse_id
                    WHERE vc.collection_id IN ({ids_sql})
                """, ids_params)
            for v in c.fetchall():
                try:
                    collection_id = int(v['collection_id'])
                    verse_payload = {"id": v['id'], "ref": v['reference'], "text": v['text']}
                except Exception:
                    collection_id = int(v[0])
                    verse_payload = {"id": v[1], "ref": v[2], "text": v[3]}
                bucket = collection_lookup.get(collection_id)
                if bucket is not None:
                    bucket["verses"].append(verse_payload)
        
        favorites = next((c for c in collections if (c.get("name") or "").lower() == "favorites"), None)
        return jsonify({
            "liked": liked,
            "saved": saved,
            "collections": collections,
            "liked_count": len(liked),
            "saved_count": len(saved),
            "favorites_count": len(favorites.get("verses", [])) if favorites else 0
        })
    except Exception as e:
        logger.error(f"Library error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/library/search')
def search_library_verses():
    raw_query = (request.args.get('q') or '').strip()
    if not raw_query:
        return jsonify({"query": "", "verses": [], "count": 0})

    if 'user_id' not in session:
        return jsonify({"query": raw_query, "verses": [], "count": 0})

    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned"}), 403

    query = raw_query[:80]
    query_key = _normalize_bible_book_name(query)
    query_lower = query.lower()
    hint_lower = re.sub(r'^(?:1|2|3)\s*', '', query_lower).strip()
    hint_lower = re.sub(r'^(?:first|second|third)\s+', '', hint_lower).strip()
    if not hint_lower:
        hint_lower = query_lower
    like_query = f"%{query_lower}%"
    like_hint = f"%{hint_lower}%"

    filter_book = (request.args.get('book') or '').strip()
    filter_chapter = (request.args.get('chapter') or '').strip()
    filter_status = (request.args.get('status') or '').strip().lower()
    filter_translation = (request.args.get('translation') or '').strip().lower()
    filter_date_from = (request.args.get('date_from') or '').strip()
    filter_date_to = (request.args.get('date_to') or '').strip()
    sort_mode = (request.args.get('sort') or 'canonical').strip().lower()

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        if db_type == 'postgres':
            c.execute("""
                SELECT
                    v.id,
                    v.reference,
                    v.text,
                    v.translation,
                    v.source,
                    v.book,
                    v.timestamp,
                    (SELECT MAX(l.timestamp) FROM likes l WHERE l.verse_id = v.id AND l.user_id = %s) AS liked_at,
                    (SELECT MAX(s.timestamp) FROM saves s WHERE s.verse_id = v.id AND s.user_id = %s) AS saved_at
                FROM verses v
                WHERE LOWER(COALESCE(v.book, '')) LIKE %s
                   OR LOWER(COALESCE(v.reference, '')) LIKE %s
                   OR LOWER(COALESCE(v.book, '')) LIKE %s
                   OR LOWER(COALESCE(v.reference, '')) LIKE %s
                LIMIT 500
            """, (session['user_id'], session['user_id'], like_query, like_query, like_hint, like_hint))
        else:
            c.execute("""
                SELECT
                    v.id,
                    v.reference,
                    v.text,
                    v.translation,
                    v.source,
                    v.book,
                    v.timestamp,
                    (SELECT MAX(l.timestamp) FROM likes l WHERE l.verse_id = v.id AND l.user_id = ?) AS liked_at,
                    (SELECT MAX(s.timestamp) FROM saves s WHERE s.verse_id = v.id AND s.user_id = ?) AS saved_at
                FROM verses v
                WHERE LOWER(IFNULL(v.book, '')) LIKE ?
                   OR LOWER(IFNULL(v.reference, '')) LIKE ?
                   OR LOWER(IFNULL(v.book, '')) LIKE ?
                   OR LOWER(IFNULL(v.reference, '')) LIKE ?
                LIMIT 500
            """, (session['user_id'], session['user_id'], like_query, like_query, like_hint, like_hint))

        verses = []
        for row in c.fetchall():
            try:
                entry = {
                    "id": row['id'],
                    "ref": row['reference'],
                    "text": row['text'],
                    "trans": row['translation'],
                    "source": row['source'],
                    "book": row['book'],
                    "timestamp": row_value(row, 'timestamp'),
                    "liked_at": row['liked_at'],
                    "saved_at": row['saved_at']
                }
            except (TypeError, KeyError):
                entry = {
                    "id": row[0],
                    "ref": row[1],
                    "text": row[2],
                    "trans": row[3],
                    "source": row[4],
                    "book": row[5],
                    "timestamp": row[6],
                    "liked_at": row[7],
                    "saved_at": row[8]
                }
            entry["is_active"] = bool(entry.get("liked_at"))
            entry["is_stored"] = bool(entry.get("saved_at"))
            verses.append(entry)

        filtered = [v for v in verses if _verse_matches_title_query(v, query_key)]

        if filter_book:
            book_key = _normalize_bible_book_name(filter_book)
            filtered = [
                v for v in filtered
                if _normalize_bible_book_name(v.get("book") or _extract_book_from_reference(v.get("ref"))) == book_key
            ]

        if filter_chapter.isdigit():
            chapter_value = int(filter_chapter)
            filtered = [v for v in filtered if _parse_reference_chapter(v.get("ref"))[0] == chapter_value]

        if filter_status in ('active', 'stored'):
            if filter_status == 'active':
                filtered = [v for v in filtered if bool(v.get("liked_at"))]
            else:
                filtered = [v for v in filtered if bool(v.get("saved_at"))]

        if filter_translation:
            filtered = [
                v for v in filtered
                if str(v.get("trans") or "").strip().lower() == filter_translation
            ]

        if filter_date_from:
            filtered = [v for v in filtered if str(v.get("timestamp") or "") >= filter_date_from]

        if filter_date_to:
            filtered = [v for v in filtered if str(v.get("timestamp") or "") <= filter_date_to]

        deduped = []
        seen_ids = set()
        seen_content = set()
        for verse in filtered:
            verse_id = verse.get("id")
            if verse_id in seen_ids:
                continue
            content_key = (
                _normalize_bible_book_name(verse.get("ref")),
                _normalize_mem_text(verse.get("text"))
            )
            if content_key in seen_content:
                continue
            seen_ids.add(verse_id)
            seen_content.add(content_key)
            deduped.append(verse)

        if sort_mode == 'newest':
            deduped.sort(key=lambda v: str(v.get("timestamp") or ''), reverse=True)
        elif sort_mode == 'oldest':
            deduped.sort(key=lambda v: str(v.get("timestamp") or ''))
        elif sort_mode == 'az':
            deduped.sort(key=lambda v: str(v.get("ref") or '').lower())
        elif sort_mode == 'book':
            deduped.sort(key=_library_verse_sort_key)
        else:
            deduped.sort(key=_library_verse_sort_key)

        return jsonify({
            "query": query,
            "filters": {
                "book": filter_book,
                "chapter": filter_chapter,
                "status": filter_status,
                "translation": filter_translation,
                "date_from": filter_date_from,
                "date_to": filter_date_to,
                "sort": sort_mode
            },
            "verses": deduped,
            "count": len(deduped)
        })
    except Exception as e:
        logger.error(f"Library search error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/reading-plan', methods=['GET', 'POST'])
def reading_plan():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    user_id = session['user_id']
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    now_iso = datetime.now().isoformat()
    try:
        ensure_research_feature_tables(c, db_type)
        conn.commit()

        if request.method == 'POST':
            data = request.get_json(silent=True) or {}
            action = (data.get('action') or '').strip().lower()

            if action == 'toggle_day':
                day = int(data.get('day') or 0)
                if day <= 0:
                    return jsonify({"error": "day must be >= 1"}), 400
                if db_type == 'postgres':
                    c.execute("""
                        SELECT id, plan_days, progress_json
                        FROM reading_plans
                        WHERE user_id = %s
                        ORDER BY id DESC
                        LIMIT 1
                    """, (user_id,))
                else:
                    c.execute("""
                        SELECT id, plan_days, progress_json
                        FROM reading_plans
                        WHERE user_id = ?
                        ORDER BY id DESC
                        LIMIT 1
                    """, (user_id,))
                row = c.fetchone()
                if not row:
                    return jsonify({"error": "No reading plan found"}), 404
                plan_id = row_pick(row, 'id', 0)
                plan_days = int(row_pick(row, 'plan_days', 1, 0) or 0)
                progress = _json_loads_safe(row_pick(row, 'progress_json', 2, {}), {})
                completed = set(progress.get('completed_days') or [])
                if day in completed:
                    completed.remove(day)
                else:
                    completed.add(day)
                progress['completed_days'] = sorted([d for d in completed if isinstance(d, int) and 1 <= d <= plan_days])
                progress['last_updated'] = now_iso

                if db_type == 'postgres':
                    c.execute("""
                        UPDATE reading_plans
                        SET progress_json = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                    """, (json.dumps(progress), plan_id))
                else:
                    c.execute("""
                        UPDATE reading_plans
                        SET progress_json = ?, updated_at = ?
                        WHERE id = ?
                    """, (json.dumps(progress), now_iso, plan_id))
                conn.commit()
            else:
                plan_name = (data.get('plan_name') or 'Research Plan').strip()[:80]
                plan_days = int(data.get('plan_days') or 7)
                plan_days = max(1, min(365, plan_days))
                start_date = (data.get('start_date') or datetime.now().date().isoformat()).strip()

                c.execute("""
                    SELECT id, reference, text, translation, source, timestamp, book
                    FROM verses
                    ORDER BY id DESC
                    LIMIT 1200
                """)
                raw_verses = c.fetchall()
                verses = []
                seen = set()
                for row in raw_verses:
                    item = {
                        "id": row_pick(row, 'id', 0),
                        "ref": row_pick(row, 'reference', 1),
                        "text": row_pick(row, 'text', 2),
                        "trans": row_pick(row, 'translation', 3),
                        "source": row_pick(row, 'source', 4),
                        "timestamp": row_pick(row, 'timestamp', 5),
                        "book": row_pick(row, 'book', 6)
                    }
                    content_key = (
                        _normalize_bible_book_name(item.get("ref")),
                        _normalize_mem_text(item.get("text"))
                    )
                    if content_key in seen:
                        continue
                    seen.add(content_key)
                    verses.append(item)
                verses.sort(key=_library_verse_sort_key)

                if not verses:
                    return jsonify({"error": "No verses in database to build a plan"}), 400

                per_day = max(1, len(verses) // plan_days)
                days = []
                index = 0
                for day in range(1, plan_days + 1):
                    start = index
                    end = min(len(verses), start + per_day)
                    if day == plan_days:
                        end = len(verses)
                    day_refs = [v["ref"] for v in verses[start:end] if v.get("ref")]
                    if not day_refs and verses:
                        day_refs = [verses[min(start, len(verses)-1)].get("ref")]
                    days.append({"day": day, "references": day_refs})
                    index = end
                    if index >= len(verses):
                        index = len(verses)

                progress = {
                    "completed_days": [],
                    "schedule": days,
                    "created_at": now_iso
                }
                if db_type == 'postgres':
                    c.execute("""
                        INSERT INTO reading_plans (user_id, plan_name, plan_days, start_date, progress_json, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """, (user_id, plan_name, plan_days, start_date, json.dumps(progress)))
                else:
                    c.execute("""
                        INSERT INTO reading_plans (user_id, plan_name, plan_days, start_date, progress_json, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (user_id, plan_name, plan_days, start_date, json.dumps(progress), now_iso, now_iso))
                conn.commit()

        if db_type == 'postgres':
            c.execute("""
                SELECT id, plan_name, plan_days, start_date, progress_json, created_at, updated_at
                FROM reading_plans
                WHERE user_id = %s
                ORDER BY id DESC
                LIMIT 1
            """, (user_id,))
        else:
            c.execute("""
                SELECT id, plan_name, plan_days, start_date, progress_json, created_at, updated_at
                FROM reading_plans
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT 1
            """, (user_id,))
        row = c.fetchone()
        if not row:
            return jsonify({"plan": None})

        progress = _json_loads_safe(row_pick(row, 'progress_json', 4, {}), {})
        completed_days = progress.get("completed_days") or []
        schedule = progress.get("schedule") or []
        plan_days = int(row_pick(row, 'plan_days', 2, 0) or 0)
        done_count = len([d for d in completed_days if isinstance(d, int)])
        percent = round((done_count / max(1, plan_days)) * 100, 2)

        return jsonify({
            "plan": {
                "id": row_pick(row, 'id', 0),
                "plan_name": row_pick(row, 'plan_name', 1),
                "plan_days": plan_days,
                "start_date": row_pick(row, 'start_date', 3),
                "created_at": row_pick(row, 'created_at', 5),
                "updated_at": row_pick(row, 'updated_at', 6),
                "completed_days": completed_days,
                "schedule": schedule,
                "progress_percent": percent
            }
        })
    except Exception as e:
        logger.error(f"Reading plan error: {e}")
        return jsonify({"error": "reading_plan_failed"}), 500
    finally:
        conn.close()

@app.route('/api/memorization/trainer')
def memorization_trainer():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    user_id = session['user_id']
    verse_id = request.args.get('verse_id')
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        ensure_research_feature_tables(c, db_type)
        conn.commit()

        if verse_id and str(verse_id).isdigit():
            c.execute("""
                SELECT id, reference, text, translation, source, timestamp, book
                FROM verses WHERE id = ?
            """ if db_type != 'postgres' else """
                SELECT id, reference, text, translation, source, timestamp, book
                FROM verses WHERE id = %s
            """, (int(verse_id),))
            row = c.fetchone()
        else:
            c.execute("""
                SELECT v.id, v.reference, v.text, v.translation, v.source, v.timestamp, v.book
                FROM verses v
                WHERE EXISTS (SELECT 1 FROM likes l WHERE l.verse_id = v.id AND l.user_id = ?)
                   OR EXISTS (SELECT 1 FROM saves s WHERE s.verse_id = v.id AND s.user_id = ?)
                ORDER BY RANDOM()
                LIMIT 1
            """ if db_type != 'postgres' else """
                SELECT v.id, v.reference, v.text, v.translation, v.source, v.timestamp, v.book
                FROM verses v
                WHERE EXISTS (SELECT 1 FROM likes l WHERE l.verse_id = v.id AND l.user_id = %s)
                   OR EXISTS (SELECT 1 FROM saves s WHERE s.verse_id = v.id AND s.user_id = %s)
                ORDER BY RANDOM()
                LIMIT 1
            """, (user_id, user_id))
            row = c.fetchone()
            if not row:
                c.execute("""
                    SELECT id, reference, text, translation, source, timestamp, book
                    FROM verses
                    ORDER BY RANDOM()
                    LIMIT 1
                """)
                row = c.fetchone()

        if not row:
            return jsonify({"error": "No verses available"}), 404

        item = {
            "id": row_pick(row, 'id', 0),
            "ref": row_pick(row, 'reference', 1),
            "text": row_pick(row, 'text', 2),
            "trans": row_pick(row, 'translation', 3),
            "source": row_pick(row, 'source', 4),
            "timestamp": row_pick(row, 'timestamp', 5),
            "book": row_pick(row, 'book', 6)
        }

        c.execute("""
            SELECT best_accuracy, attempts
            FROM memorization_scores
            WHERE user_id = ? AND verse_id = ?
        """ if db_type != 'postgres' else """
            SELECT best_accuracy, attempts
            FROM memorization_scores
            WHERE user_id = %s AND verse_id = %s
        """, (user_id, item["id"]))
        score_row = c.fetchone()
        best_accuracy = float(row_pick(score_row, 'best_accuracy', 0, 0.0) or 0.0) if score_row else 0.0
        attempts = int(row_pick(score_row, 'attempts', 1, 0) or 0) if score_row else 0

        return jsonify({
            "verse": item,
            "masked_text": _build_memorization_mask(item.get("text")),
            "best_accuracy": round(best_accuracy, 4),
            "attempts": attempts
        })
    except Exception as e:
        logger.error(f"Memorization trainer error: {e}")
        return jsonify({"error": "memorization_trainer_failed"}), 500
    finally:
        conn.close()

@app.route('/api/memorization/trainer/check', methods=['POST'])
def memorization_trainer_check():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json(silent=True) or {}
    verse_id = data.get('verse_id')
    attempt_text = data.get('attempt_text') or ''
    if not verse_id or not str(verse_id).isdigit():
        return jsonify({"error": "verse_id is required"}), 400

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    now_iso = datetime.now().isoformat()
    try:
        ensure_research_feature_tables(c, db_type)
        conn.commit()

        c.execute("SELECT text FROM verses WHERE id = ?" if db_type != 'postgres' else "SELECT text FROM verses WHERE id = %s", (int(verse_id),))
        row = c.fetchone()
        if not row:
            return jsonify({"error": "Verse not found"}), 404

        verse_text = row_pick(row, 'text', 0) or ''
        accuracy = _compute_text_similarity(verse_text, attempt_text)

        if db_type == 'postgres':
            c.execute("""
                INSERT INTO memorization_scores (user_id, verse_id, accuracy, attempts, best_accuracy, updated_at)
                VALUES (%s, %s, %s, 1, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id, verse_id) DO UPDATE SET
                    accuracy = EXCLUDED.accuracy,
                    attempts = memorization_scores.attempts + 1,
                    best_accuracy = GREATEST(memorization_scores.best_accuracy, EXCLUDED.accuracy),
                    updated_at = CURRENT_TIMESTAMP
            """, (session['user_id'], int(verse_id), accuracy, accuracy))
            c.execute("SELECT best_accuracy, attempts FROM memorization_scores WHERE user_id = %s AND verse_id = %s", (session['user_id'], int(verse_id)))
        else:
            c.execute("""
                INSERT INTO memorization_scores (user_id, verse_id, accuracy, attempts, best_accuracy, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(user_id, verse_id) DO UPDATE SET
                    accuracy = excluded.accuracy,
                    attempts = memorization_scores.attempts + 1,
                    best_accuracy = CASE
                        WHEN excluded.accuracy > memorization_scores.best_accuracy THEN excluded.accuracy
                        ELSE memorization_scores.best_accuracy
                    END,
                    updated_at = excluded.updated_at
            """, (session['user_id'], int(verse_id), accuracy, accuracy, now_iso))
            c.execute("SELECT best_accuracy, attempts FROM memorization_scores WHERE user_id = ? AND verse_id = ?", (session['user_id'], int(verse_id)))

        score_row = c.fetchone()
        conn.commit()
        best_accuracy = float(row_pick(score_row, 'best_accuracy', 0, accuracy) or accuracy) if score_row else accuracy
        attempts = int(row_pick(score_row, 'attempts', 1, 1) or 1) if score_row else 1
        return jsonify({
            "accuracy": round(accuracy, 4),
            "accuracy_percent": round(accuracy * 100, 2),
            "best_accuracy_percent": round(best_accuracy * 100, 2),
            "attempts": attempts
        })
    except Exception as e:
        logger.error(f"Memorization check error: {e}")
        return jsonify({"error": "memorization_check_failed"}), 500
    finally:
        conn.close()

@app.route('/api/highlights', methods=['GET', 'POST'])
def highlights():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    user_id = session['user_id']
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    now_iso = datetime.now().isoformat()
    try:
        ensure_research_feature_tables(c, db_type)
        conn.commit()

        if request.method == 'POST':
            data = request.get_json(silent=True) or {}
            verse_id = data.get('verse_id')
            if not verse_id or not str(verse_id).isdigit():
                return jsonify({"error": "verse_id is required"}), 400
            color = (data.get('color') or '#FFD54F').strip()[:24]
            note = (data.get('note') or '').strip()[:600]
            if db_type == 'postgres':
                c.execute("""
                    INSERT INTO verse_highlights (user_id, verse_id, color, note, created_at)
                    VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (user_id, verse_id) DO UPDATE SET
                        color = EXCLUDED.color,
                        note = EXCLUDED.note
                """, (user_id, int(verse_id), color, note))
            else:
                c.execute("""
                    INSERT INTO verse_highlights (user_id, verse_id, color, note, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, verse_id) DO UPDATE SET
                        color = excluded.color,
                        note = excluded.note
                """, (user_id, int(verse_id), color, note, now_iso))
            conn.commit()

        c.execute("""
            SELECT h.verse_id, h.color, h.note, h.created_at,
                   v.reference, v.text, v.translation, v.source, v.book
            FROM verse_highlights h
            JOIN verses v ON v.id = h.verse_id
            WHERE h.user_id = ?
            ORDER BY h.created_at DESC
        """ if db_type != 'postgres' else """
            SELECT h.verse_id, h.color, h.note, h.created_at,
                   v.reference, v.text, v.translation, v.source, v.book
            FROM verse_highlights h
            JOIN verses v ON v.id = h.verse_id
            WHERE h.user_id = %s
            ORDER BY h.created_at DESC
        """, (user_id,))
        results = []
        for row in c.fetchall():
            results.append({
                "verse_id": row_pick(row, 'verse_id', 0),
                "color": row_pick(row, 'color', 1),
                "note": row_pick(row, 'note', 2),
                "created_at": row_pick(row, 'created_at', 3),
                "ref": row_pick(row, 'reference', 4),
                "text": row_pick(row, 'text', 5),
                "trans": row_pick(row, 'translation', 6),
                "source": row_pick(row, 'source', 7),
                "book": row_pick(row, 'book', 8)
            })
        return jsonify({"highlights": results, "count": len(results)})
    except Exception as e:
        logger.error(f"Highlights error: {e}")
        return jsonify({"error": "highlights_failed"}), 500
    finally:
        conn.close()

@app.route('/api/highlights/<int:verse_id>', methods=['DELETE'])
def remove_highlight(verse_id):
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        ensure_research_feature_tables(c, db_type)
        conn.commit()
        c.execute("DELETE FROM verse_highlights WHERE user_id = ? AND verse_id = ?" if db_type != 'postgres' else "DELETE FROM verse_highlights WHERE user_id = %s AND verse_id = %s", (session['user_id'], verse_id))
        deleted = c.rowcount if c.rowcount and c.rowcount > 0 else 0
        conn.commit()
        return jsonify({"success": True, "deleted": deleted})
    except Exception as e:
        logger.error(f"Remove highlight error: {e}")
        return jsonify({"error": "remove_highlight_failed"}), 500
    finally:
        conn.close()

@app.route('/api/study-pack/export')
def study_pack_export():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    include_highlights = str(request.args.get('include_highlights', '1')).lower() in ('1', 'true', 'yes', 'on')
    include_library = str(request.args.get('include_library', '1')).lower() in ('1', 'true', 'yes', 'on')
    fmt = (request.args.get('format') or 'markdown').strip().lower()
    user_id = session['user_id']

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        ensure_research_feature_tables(c, db_type)
        conn.commit()

        library_rows = []
        if include_library:
            c.execute("""
                SELECT DISTINCT v.id, v.reference, v.text, v.translation, v.source, v.book
                FROM verses v
                WHERE EXISTS (SELECT 1 FROM likes l WHERE l.verse_id = v.id AND l.user_id = ?)
                   OR EXISTS (SELECT 1 FROM saves s WHERE s.verse_id = v.id AND s.user_id = ?)
                ORDER BY v.id DESC
                LIMIT 1000
            """ if db_type != 'postgres' else """
                SELECT DISTINCT v.id, v.reference, v.text, v.translation, v.source, v.book
                FROM verses v
                WHERE EXISTS (SELECT 1 FROM likes l WHERE l.verse_id = v.id AND l.user_id = %s)
                   OR EXISTS (SELECT 1 FROM saves s WHERE s.verse_id = v.id AND s.user_id = %s)
                ORDER BY v.id DESC
                LIMIT 1000
            """, (user_id, user_id))
            library_rows = c.fetchall()

        highlight_rows = []
        if include_highlights:
            c.execute("""
                SELECT h.verse_id, h.color, h.note, h.created_at,
                       v.reference, v.text, v.translation, v.source, v.book
                FROM verse_highlights h
                JOIN verses v ON v.id = h.verse_id
                WHERE h.user_id = ?
                ORDER BY h.created_at DESC
            """ if db_type != 'postgres' else """
                SELECT h.verse_id, h.color, h.note, h.created_at,
                       v.reference, v.text, v.translation, v.source, v.book
                FROM verse_highlights h
                JOIN verses v ON v.id = h.verse_id
                WHERE h.user_id = %s
                ORDER BY h.created_at DESC
            """, (user_id,))
            highlight_rows = c.fetchall()

        library_items = []
        seen = set()
        for row in library_rows:
            item = {
                "id": row_pick(row, 'id', 0),
                "ref": row_pick(row, 'reference', 1),
                "text": row_pick(row, 'text', 2),
                "trans": row_pick(row, 'translation', 3),
                "source": row_pick(row, 'source', 4),
                "book": row_pick(row, 'book', 5),
            }
            key = (_normalize_bible_book_name(item["ref"]), _normalize_mem_text(item["text"]))
            if key in seen:
                continue
            seen.add(key)
            library_items.append(item)
        library_items.sort(key=_library_verse_sort_key)

        highlights = []
        for row in highlight_rows:
            highlights.append({
                "verse_id": row_pick(row, 'verse_id', 0),
                "color": row_pick(row, 'color', 1),
                "note": row_pick(row, 'note', 2),
                "created_at": row_pick(row, 'created_at', 3),
                "ref": row_pick(row, 'reference', 4),
                "text": row_pick(row, 'text', 5),
                "trans": row_pick(row, 'translation', 6),
                "source": row_pick(row, 'source', 7),
                "book": row_pick(row, 'book', 8),
            })
        highlights.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)

        if fmt == 'json':
            return jsonify({
                "generated_at": datetime.now().isoformat(),
                "library": library_items,
                "highlights": highlights
            })

        lines = []
        lines.append(f"# Study Pack Export ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
        lines.append("")
        lines.append("## Library Verses")
        if library_items:
            for item in library_items:
                lines.append(f"- **{item.get('ref') or 'Unknown'}**: {item.get('text') or ''}")
        else:
            lines.append("- No library verses.")
        lines.append("")
        lines.append("## Highlights")
        if highlights:
            for item in highlights:
                note = item.get('note') or ''
                lines.append(f"- **{item.get('ref') or 'Unknown'}** ({item.get('color') or '#FFD54F'}): {item.get('text') or ''}")
                if note:
                    lines.append(f"  Note: {note}")
        else:
            lines.append("- No highlights.")

        return jsonify({
            "format": "markdown",
            "generated_at": datetime.now().isoformat(),
            "content": "\n".join(lines)
        })
    except Exception as e:
        logger.error(f"Study pack export error: {e}")
        return jsonify({"error": "study_pack_export_failed"}), 500
    finally:
        conn.close()

@app.route('/api/admin/data-health')
def admin_data_health():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    if not session.get('is_admin') and not require_min_role('host'):
        return jsonify({"error": "Admin required"}), 403

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        ensure_research_feature_tables(c, db_type)
        conn.commit()

        if db_type == 'postgres':
            c.execute("""
                SELECT COUNT(*) AS duplicate_groups, COALESCE(SUM(cnt - 1), 0) AS duplicate_rows
                FROM (
                    SELECT LOWER(TRIM(COALESCE(reference, ''))) AS ref_key,
                           LOWER(TRIM(COALESCE(text, ''))) AS text_key,
                           COUNT(*) AS cnt
                    FROM verses
                    GROUP BY ref_key, text_key
                    HAVING COUNT(*) > 1
                ) d
            """)
            dup = c.fetchone()
            duplicate_groups = int(row_pick(dup, 'duplicate_groups', 0, 0) or 0) if dup else 0
            duplicate_rows = int(row_pick(dup, 'duplicate_rows', 1, 0) or 0) if dup else 0
            c.execute("SELECT COUNT(*) AS count FROM likes l LEFT JOIN verses v ON l.verse_id = v.id WHERE v.id IS NULL")
            row = c.fetchone()
            orphan_likes = int(row_pick(row, 'count', 0, 0) or 0) if row else 0
            c.execute("SELECT COUNT(*) AS count FROM saves s LEFT JOIN verses v ON s.verse_id = v.id WHERE v.id IS NULL")
            row = c.fetchone()
            orphan_saves = int(row_pick(row, 'count', 0, 0) or 0) if row else 0
        else:
            c.execute("""
                SELECT COUNT(*), COALESCE(SUM(cnt - 1), 0)
                FROM (
                    SELECT LOWER(TRIM(IFNULL(reference, ''))) AS ref_key,
                           LOWER(TRIM(IFNULL(text, ''))) AS text_key,
                           COUNT(*) AS cnt
                    FROM verses
                    GROUP BY ref_key, text_key
                    HAVING COUNT(*) > 1
                ) d
            """)
            row = c.fetchone()
            duplicate_groups = int(row[0] if row else 0)
            duplicate_rows = int(row[1] if row else 0)
            c.execute("SELECT COUNT(*) FROM likes WHERE verse_id NOT IN (SELECT id FROM verses)")
            row = c.fetchone()
            orphan_likes = int(row[0] if row else 0)
            c.execute("SELECT COUNT(*) FROM saves WHERE verse_id NOT IN (SELECT id FROM verses)")
            row = c.fetchone()
            orphan_saves = int(row[0] if row else 0)

        c.execute("SELECT COUNT(*) FROM verses")
        row = c.fetchone()
        total_verses = int(row[0] if row else 0)

        return jsonify({
            "db_type": db_type,
            "total_verses": total_verses,
            "duplicate_groups": duplicate_groups,
            "duplicate_rows": duplicate_rows,
            "orphan_likes": orphan_likes,
            "orphan_saves": orphan_saves
        })
    except Exception as e:
        logger.error(f"Data health error: {e}")
        return jsonify({"error": "data_health_failed"}), 500
    finally:
        conn.close()

@app.route('/api/admin/data-health/repair', methods=['POST'])
def admin_data_health_repair():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    if not session.get('is_admin') and not require_min_role('host'):
        return jsonify({"error": "Admin required"}), 403

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        ensure_research_feature_tables(c, db_type)
        conn.commit()
        deduped_count = _dedupe_verses_in_db(c, db_type)
        orphan = _remove_orphan_verse_refs(c, db_type)
        conn.commit()
        return jsonify({
            "success": True,
            "deduped_verses_removed": deduped_count,
            "orphan_cleanup": orphan
        })
    except Exception as e:
        conn.rollback()
        logger.error(f"Data repair error: {e}")
        return jsonify({"error": "data_repair_failed"}), 500
    finally:
        conn.close()

@app.route('/api/community/rooms')
def get_community_rooms():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        ensure_research_feature_tables(c, db_type)
        conn.commit()
        c.execute("SELECT slug, name, description FROM research_community_rooms ORDER BY id ASC")
        rows = c.fetchall()
        rooms = [{
            "slug": row_pick(r, 'slug', 0),
            "name": row_pick(r, 'name', 1),
            "description": row_pick(r, 'description', 2)
        } for r in rows]
        return jsonify({"rooms": rooms})
    except Exception as e:
        logger.error(f"Community rooms error: {e}")
        return jsonify({"error": "community_rooms_failed"}), 500
    finally:
        conn.close()

@app.route('/api/community/rooms/<slug>/messages', methods=['GET', 'POST'])
def community_room_messages(slug):
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    room_slug = (slug or '').strip().lower()
    if not room_slug:
        return jsonify({"error": "Invalid room"}), 400

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        ensure_research_feature_tables(c, db_type)
        conn.commit()

        if request.method == 'POST':
            is_banned, _, _ = check_ban_status(session['user_id'])
            if is_banned:
                return jsonify({"error": "banned", "message": "Account banned"}), 403
            data = request.get_json(silent=True) or {}
            text = (data.get('text') or '').strip()
            if not text:
                return jsonify({"error": "Empty message"}), 400
            c.execute("""
                INSERT INTO research_community_messages (room_slug, user_id, text, timestamp, google_name, google_picture)
                VALUES (?, ?, ?, ?, ?, ?)
            """ if db_type != 'postgres' else """
                INSERT INTO research_community_messages (room_slug, user_id, text, timestamp, google_name, google_picture)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (room_slug, session['user_id'], text, datetime.now().isoformat(), session.get('user_name'), session.get('user_picture')))
            conn.commit()

        c.execute("""
            SELECT m.id, m.room_slug, m.user_id, m.text, m.timestamp, m.google_name, m.google_picture,
                   u.name, COALESCE(u.custom_picture, u.picture) AS picture, u.role, u.avatar_decoration
            FROM research_community_messages m
            LEFT JOIN users u ON u.id = m.user_id
            WHERE m.room_slug = ?
            ORDER BY m.timestamp DESC
            LIMIT 120
        """ if db_type != 'postgres' else """
            SELECT m.id, m.room_slug, m.user_id, m.text, m.timestamp, m.google_name, m.google_picture,
                   u.name, COALESCE(u.custom_picture, u.picture) AS picture, u.role, u.avatar_decoration
            FROM research_community_messages m
            LEFT JOIN users u ON u.id = m.user_id
            WHERE m.room_slug = %s
            ORDER BY m.timestamp DESC
            LIMIT 120
        """, (room_slug,))
        rows = c.fetchall()
        messages = []
        for row in rows:
            messages.append({
                "id": row_pick(row, 'id', 0),
                "room_slug": row_pick(row, 'room_slug', 1),
                "user_id": row_pick(row, 'user_id', 2),
                "text": row_pick(row, 'text', 3),
                "timestamp": row_pick(row, 'timestamp', 4),
                "user_name": row_pick(row, 'name', 7) or row_pick(row, 'google_name', 5) or "Anonymous",
                "user_picture": row_pick(row, 'picture', 8) or row_pick(row, 'google_picture', 6) or "",
                "user_role": normalize_role(row_pick(row, 'role', 9) or 'user'),
                "avatar_decoration": row_pick(row, 'avatar_decoration', 10) or ""
            })
        return jsonify({"room": room_slug, "messages": messages})
    except Exception as e:
        logger.error(f"Community room messages error: {e}")
        return jsonify({"error": "community_room_failed"}), 500
    finally:
        conn.close()

@app.route('/api/collections/add', methods=['POST'])
def add_to_collection():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned"}), 403
    
    data = request.get_json()
    collection_id = data.get('collection_id')
    verse_id = data.get('verse_id')
    
    if not collection_id or not verse_id:
        return jsonify({"success": False, "error": "Missing collection_id or verse_id"}), 400
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        # Verify collection belongs to user
        if db_type == 'postgres':
            c.execute("SELECT user_id FROM collections WHERE id = %s", (collection_id,))
        else:
            c.execute("SELECT user_id FROM collections WHERE id = ?", (collection_id,))
        
        row = c.fetchone()
        if not row:
            return jsonify({"success": False, "error": "Collection not found"}), 404
        
        try:
            owner_id = row['user_id'] if isinstance(row, dict) else row[0]
        except (TypeError, KeyError):
            owner_id = row[0]
        
        if owner_id != session['user_id']:
            return jsonify({"success": False, "error": "Not your collection"}), 403
        
        # Add verse to collection
        if db_type == 'postgres':
            c.execute("INSERT INTO verse_collections (collection_id, verse_id) VALUES (%s, %s)",
                      (collection_id, verse_id))
        else:
            c.execute("INSERT INTO verse_collections (collection_id, verse_id) VALUES (?, ?)",
                      (collection_id, verse_id))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Add to collection error: {e}")
        # Likely already exists
        return jsonify({"success": False, "error": "Already in collection or database error"})
    finally:
        conn.close()

@app.route('/api/collections/create', methods=['POST'])
def create_collection():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned"}), 403
    
    data = request.get_json()
    name = data.get('name')
    color = data.get('color', '#0A84FF')
    
    if not name:
        return jsonify({"error": "Name required"}), 400
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        if db_type == 'postgres':
            c.execute("INSERT INTO collections (user_id, name, color, created_at) VALUES (%s, %s, %s, %s) RETURNING id",
                      (session['user_id'], name, color, datetime.now().isoformat()))
            new_id = c.fetchone()['id']
        else:
            c.execute("INSERT INTO collections (user_id, name, color, created_at) VALUES (?, ?, ?, ?)",
                      (session['user_id'], name, color, datetime.now().isoformat()))
            new_id = c.lastrowid
        
        conn.commit()
        return jsonify({"id": new_id, "name": name, "color": color, "count": 0, "verses": []})
    except Exception as e:
        logger.error(f"Create collection error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/recommendations')
def get_recommendations():
    if 'user_id' not in session:
        return jsonify([])
    
    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned"}), 403
    
    rec = generator.generate_smart_recommendation(session['user_id'])
    if rec:
        return jsonify({"recommendations": [rec]})
    return jsonify({"recommendations": []})

@app.route('/api/mood/<mood>')
def get_mood_recommendation(mood):
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned"}), 403

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        mood_key = str(mood or '').strip().lower()
        keywords = MOOD_KEYWORDS.get(mood_key) or MOOD_KEYWORDS.get('peace', [])
        exclude_raw = request.args.get('exclude', '')
        exclude_ids = []
        for raw in str(exclude_raw).split(','):
            raw = raw.strip()
            if raw.isdigit():
                exclude_ids.append(int(raw))
        exclude_ids = list(dict.fromkeys(exclude_ids))
        row = None
        if keywords:
            if db_type == 'postgres':
                clauses = " OR ".join(["text ILIKE %s"] * len(keywords))
                params = [f"%{k}%" for k in keywords]
                exclude_clause = ''
                if exclude_ids:
                    exclude_clause = f" AND id NOT IN ({','.join(['%s'] * len(exclude_ids))})"
                    params.extend(exclude_ids)
                c.execute(f"""
                    SELECT id, reference, text, translation, book
                    FROM verses
                    WHERE {clauses}
                    {exclude_clause}
                    ORDER BY RANDOM()
                    LIMIT 1
                """, params)
            else:
                clauses = " OR ".join(["text LIKE ?"] * len(keywords))
                params = [f"%{k}%" for k in keywords]
                exclude_clause = ''
                if exclude_ids:
                    exclude_clause = f" AND id NOT IN ({','.join('?' for _ in exclude_ids)})"
                    params.extend(exclude_ids)
                c.execute(f"""
                    SELECT id, reference, text, translation, book
                    FROM verses
                    WHERE {clauses}
                    {exclude_clause}
                    ORDER BY RANDOM()
                    LIMIT 1
                """, params)
            row = c.fetchone()

        if not row:
            if db_type == 'postgres':
                if exclude_ids:
                    c.execute(f"""
                        SELECT id, reference, text, translation, book
                        FROM verses
                        WHERE id NOT IN ({','.join(['%s'] * len(exclude_ids))})
                        ORDER BY RANDOM() LIMIT 1
                    """, exclude_ids)
                else:
                    c.execute("SELECT id, reference, text, translation, book FROM verses ORDER BY RANDOM() LIMIT 1")
            else:
                if exclude_ids:
                    c.execute(f"""
                        SELECT id, reference, text, translation, book
                        FROM verses
                        WHERE id NOT IN ({','.join('?' for _ in exclude_ids)})
                        ORDER BY RANDOM() LIMIT 1
                    """, exclude_ids)
                else:
                    c.execute("SELECT id, reference, text, translation, book FROM verses ORDER BY RANDOM() LIMIT 1")
            row = c.fetchone()

        if not row:
            return jsonify({"error": "No verses found"}), 404

        try:
            return jsonify({
                "id": row['id'],
                "ref": row['reference'],
                "text": row['text'],
                "trans": row['translation'],
                "book": row['book']
            })
        except (TypeError, KeyError):
            return jsonify({
                "id": row[0],
                "ref": row[1],
                "text": row[2],
                "trans": row[3],
                "book": row[4] if len(row) > 4 else ''
            })
    except Exception as e:
        logger.error(f"Mood recommendation error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/user/avatar', methods=['POST'])
def update_user_avatar():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    if not require_min_role('host'):
        return jsonify({"error": "Host+ required"}), 403

    data = request.get_json() or {}
    kind = (data.get('kind') or 'picture').strip().lower()
    url = (data.get('url') or '').strip()
    reset = bool(data.get('reset'))
    if kind not in ('picture', 'decoration'):
        return jsonify({"error": "Invalid kind"}), 400

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        if kind == 'picture':
            if reset:
                if db_type == 'postgres':
                    c.execute("UPDATE users SET custom_picture = NULL WHERE id = %s", (session['user_id'],))
                else:
                    c.execute("UPDATE users SET custom_picture = NULL WHERE id = ?", (session['user_id'],))
                try:
                    if db_type == 'postgres':
                        c.execute("SELECT picture FROM users WHERE id = %s", (session['user_id'],))
                    else:
                        c.execute("SELECT picture FROM users WHERE id = ?", (session['user_id'],))
                    base_row = c.fetchone()
                    base_pic = base_row['picture'] if isinstance(base_row, dict) else (base_row[0] if base_row else '')
                except Exception:
                    base_pic = session.get('user_picture') or ''
                session['user_picture'] = base_pic
            else:
                if len(url) < 6:
                    return jsonify({"error": "Invalid URL"}), 400
                if db_type == 'postgres':
                    c.execute("UPDATE users SET custom_picture = %s WHERE id = %s", (url, session['user_id']))
                else:
                    c.execute("UPDATE users SET custom_picture = ? WHERE id = ?", (url, session['user_id']))
                session['user_picture'] = url
        else:
            if reset:
                if db_type == 'postgres':
                    c.execute("UPDATE users SET avatar_decoration = NULL WHERE id = %s", (session['user_id'],))
                else:
                    c.execute("UPDATE users SET avatar_decoration = NULL WHERE id = ?", (session['user_id'],))
                session['avatar_decoration'] = None
            else:
                if len(url) < 6:
                    return jsonify({"error": "Invalid URL"}), 400
                if db_type == 'postgres':
                    c.execute("UPDATE users SET avatar_decoration = %s WHERE id = %s", (url, session['user_id']))
                else:
                    c.execute("UPDATE users SET avatar_decoration = ? WHERE id = ?", (url, session['user_id']))
                session['avatar_decoration'] = url

        conn.commit()
        return jsonify({
            "success": True,
            "picture": session.get('user_picture'),
            "avatar_decoration": session.get('avatar_decoration')
        })
    except Exception as e:
        logger.error(f"Update avatar error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/user/avatar-upload', methods=['POST'])
def upload_user_avatar():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    if not require_min_role('host'):
        return jsonify({"error": "Host+ required"}), 403
    kind = (request.form.get('kind') or 'picture').strip().lower()
    remove_bg = str(request.form.get('remove_bg') or '').lower() in ('1', 'true', 'yes', 'on')
    if kind not in ('picture', 'decoration'):
        return jsonify({"error": "Invalid kind"}), 400
    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400
    if not allowed_image_file(file.filename):
        return jsonify({"error": "Unsupported file type"}), 400

    ext = file.filename.rsplit('.', 1)[1].lower()
    subdir = 'avatars' if kind == 'picture' else 'decorations'
    os.makedirs(os.path.join(UPLOAD_ROOT, subdir), exist_ok=True)
    stamp = int(time.time())
    filename = secure_filename(f"user_{session['user_id']}_{stamp}.{ext}")
    filepath = os.path.join(UPLOAD_ROOT, subdir, filename)
    file.save(filepath)

    warning = None
    if kind == 'picture' and remove_bg:
        if ext != 'png':
            png_name = secure_filename(f"user_{session['user_id']}_{stamp}.png")
            png_path = os.path.join(UPLOAD_ROOT, subdir, png_name)
            try:
                os.replace(filepath, png_path)
                filepath = png_path
                filename = png_name
            except Exception:
                pass
        ok, err = try_remove_background(filepath)
        if not ok:
            warning = err or "Background removal failed"

    url = f"/static/uploads/{subdir}/{filename}"
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        if kind == 'picture':
            if db_type == 'postgres':
                c.execute("UPDATE users SET custom_picture = %s WHERE id = %s", (url, session['user_id']))
            else:
                c.execute("UPDATE users SET custom_picture = ? WHERE id = ?", (url, session['user_id']))
            session['user_picture'] = url
        else:
            if db_type == 'postgres':
                c.execute("UPDATE users SET avatar_decoration = %s WHERE id = %s", (url, session['user_id']))
            else:
                c.execute("UPDATE users SET avatar_decoration = ? WHERE id = ?", (url, session['user_id']))
            session['avatar_decoration'] = url
        conn.commit()
    finally:
        conn.close()
    payload = {
        "success": True,
        "url": url,
        "picture": session.get('user_picture'),
        "avatar_decoration": session.get('avatar_decoration')
    }
    if warning:
        payload["warning"] = warning
    return jsonify(payload)

@app.route('/api/db_status')
def db_status():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    if not session.get('is_admin'):
        return jsonify({"error": "Admin required"}), 403
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        counts = {}
        for table in [
            'users', 'verses', 'likes', 'saves', 'comments', 'comment_replies',
            'community_messages', 'direct_messages', 'dm_typing', 'audit_logs', 'daily_actions', 'notifications'
        ]:
            try:
                c.execute(f"SELECT COUNT(*) FROM {table}")
                row = c.fetchone()
                counts[table] = row[0] if row else 0
            except Exception:
                counts[table] = None
        info = {
            "db_type": db_type,
            "db_mode": DB_MODE,
            "strict_db": STRICT_DB,
            "render_env": RENDER_ENV,
            "sqlite_path": SQLITE_PATH if db_type == 'sqlite' else None,
            "database_url": _redact_db_url(DATABASE_URL) if db_type == 'postgres' else None,
            "counts": counts
        }
        conn.close()
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/db-status')
def db_status_page():
    if 'user_id' not in session:
        return redirect('/login')
    if not session.get('is_admin'):
        return redirect('/')
    return render_template('db_status.html')

@app.route('/u/<int:user_id>')
def public_profile(user_id):
    if 'user_id' not in session:
        return redirect('/login')
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        if db_type == 'postgres':
            c.execute("SELECT id, name, email, COALESCE(custom_picture, picture) AS picture, role, created_at, avatar_decoration FROM users WHERE id = %s", (user_id,))
        else:
            c.execute("SELECT id, name, email, COALESCE(custom_picture, picture) AS picture, role, created_at, avatar_decoration FROM users WHERE id = ?", (user_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return "User not found", 404
        if hasattr(row, 'keys'):
            name = row['name'] or 'User'
            email = row['email'] or ''
            picture = row['picture'] or ''
            role = normalize_role(row['role'] or 'user')
            created_at = row['created_at'] or ''
            avatar_decoration = row_value(row, 'avatar_decoration')
            uid = row['id']
        else:
            uid = row[0]
            name = row[1] or 'User'
            email = row[2] or ''
            picture = row[3] or ''
            role = normalize_role(row[4] if len(row) > 4 else 'user')
            created_at = row[5] if len(row) > 5 else ''
            avatar_decoration = row[6] if len(row) > 6 else None
        viewer_role = normalize_role(session.get('admin_role') or session.get('role') or 'user')
        show_email = role_priority(viewer_role) >= role_priority('host')
        can_dm = session.get('user_id') != uid
        created_display = ''
        if created_at:
            try:
                dt = datetime.fromisoformat(str(created_at).replace('Z', ''))
                created_display = dt.strftime('%b %d, %Y')
            except Exception:
                created_display = str(created_at)
        
        # Get user's equipped items for display
        equipped = get_user_equipped_items(c, db_type, user_id)
        
        conn.close()
        return render_template('public_profile.html', user={
            "id": uid,
            "name": name,
            "email": email,
            "picture": picture,
            "avatar_decoration": avatar_decoration,
            "role": role,
            "role_display": role.replace('_', ' ').upper(),
            "created_at": created_at,
            "created_at_display": created_display,
            "equipped_frame": equipped["frame"],
            "equipped_name_color": equipped["name_color"],
            "equipped_title": equipped["title"],
            "equipped_badges": equipped["badges"],
            "equipped_profile_bg": equipped["profile_bg"]
        }, show_email=show_email, can_dm=can_dm)
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return str(e), 500

@app.route('/api/db_check')
def db_check():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    if not session.get('is_admin'):
        return jsonify({"error": "Admin required"}), 403
    try:
        conn, db_type = get_db()
        tables = _list_tables(conn, db_type)
        required = [
            'users', 'verses', 'likes', 'saves', 'comments', 'comment_replies',
            'community_messages', 'direct_messages', 'dm_typing', 'audit_logs', 'daily_actions', 'notifications'
        ]
        missing_tables = [t for t in required if t not in tables]
        table_info = {}
        for t in required:
            if t not in tables:
                table_info[t] = {"exists": False, "count": None, "columns": []}
                continue
            cols = _table_columns(conn, db_type, t)
            try:
                c = conn.cursor()
                c.execute(f"SELECT COUNT(*) FROM {t}")
                row = c.fetchone()
                count = row[0] if row else 0
            except Exception:
                count = None
            table_info[t] = {"exists": True, "count": count, "columns": cols}
        conn.close()
        return jsonify({
            "db_type": db_type,
            "db_mode": DB_MODE,
            "sqlite_path": SQLITE_PATH if db_type == 'sqlite' else None,
            "database_url": _redact_db_url(DATABASE_URL) if db_type == 'postgres' else None,
            "missing_tables": missing_tables,
            "tables": table_info
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _request_location_snapshot():
    forwarded_for = request.headers.get('X-Forwarded-For', '')
    ip = (forwarded_for.split(',')[0].strip() if forwarded_for else '') or request.headers.get('X-Real-IP') or request.remote_addr or 'unknown'
    return {
        "ip": ip,
        "country": request.headers.get('CF-IPCountry') or request.headers.get('X-Country-Code') or "",
        "region": request.headers.get('X-Region') or request.headers.get('CF-Region') or "",
        "city": request.headers.get('X-City') or request.headers.get('CF-IPCity') or "",
        "timezone": request.headers.get('CF-Timezone') or ""
    }

def log_user_activity(action, user_id=None, message=None, extras=None):
    """Write user activity into audit_logs and user_activity_logs for comprehensive tracking."""
    try:
        conn, db_type = get_db()
        c = get_cursor(conn, db_type)
        
        # Get client info
        client_ip = request.remote_addr or request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or 'unknown'
        user_agent = request.headers.get('User-Agent', '')[:500]
        now_ts = datetime.now().isoformat()
        
        # Get user details from database if user_id is provided
        google_id = None
        email = None
        user_name = None
        if user_id:
            try:
                if db_type == 'postgres':
                    c.execute("SELECT google_id, email, name FROM users WHERE id = %s", (user_id,))
                else:
                    c.execute("SELECT google_id, email, name FROM users WHERE id = ?", (user_id,))
                user_row = c.fetchone()
                if user_row:
                    if isinstance(user_row, dict) or hasattr(user_row, 'keys'):
                        google_id = user_row.get('google_id')
                        email = user_row.get('email')
                        user_name = user_row.get('name')
                    else:
                        google_id = user_row[0] if len(user_row) > 0 else None
                        email = user_row[1] if len(user_row) > 1 else None
                        user_name = user_row[2] if len(user_row) > 2 else None
            except Exception as e:
                logger.warning(f"Could not fetch user details for activity log: {e}")
        
        # Fallback to session if database lookup failed
        if not google_id:
            google_id = session.get('google_id') or 'unknown'
        if not email:
            email = session.get('user_email') or 'unknown'
        if not user_name:
            user_name = session.get('user_name') or 'unknown'
        
        # Create enhanced payload with full user details
        payload = {
            "message": str(message or ""),
            "status": "success",
            "location": _request_location_snapshot(),
            "extras": extras if isinstance(extras, dict) else {},
            "target": {"user_id": user_id, "google_id": google_id, "email": email, "name": user_name} if user_id is not None else {},
            "user": {
                "user_id": user_id,
                "google_id": google_id,
                "email": email,
                "name": user_name
            } if user_id is not None else {}
        }
        details_json = json.dumps(payload, ensure_ascii=False)
        
        # Also create user-specific activity details
        activity_details = {
            "message": str(message or ""),
            "extras": extras if isinstance(extras, dict) else {}
        }
        activity_details_json = json.dumps(activity_details, ensure_ascii=False)
        
        # Ensure tables exist
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
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_activity_logs (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    google_id TEXT,
                    email TEXT,
                    action TEXT NOT NULL,
                    details TEXT,
                    ip_address TEXT,
                    user_agent TEXT,
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
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_activity_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    google_id TEXT,
                    email TEXT,
                    action TEXT NOT NULL,
                    details TEXT,
                    ip_address TEXT,
                    user_agent TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        
        admin_id = str(user_id) if user_id is not None else "system"
        
        # Insert into audit_logs (for admin dashboard)
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO audit_logs (admin_id, action, target_user_id, details, ip_address, timestamp)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (admin_id, action, user_id, details_json, client_ip, now_ts))
        else:
            c.execute("""
                INSERT INTO audit_logs (admin_id, action, target_user_id, details, ip_address, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (admin_id, action, user_id, details_json, client_ip, now_ts))
        
        # Insert into user_activity_logs (for comprehensive user tracking)
        if user_id:
            if db_type == 'postgres':
                c.execute("""
                    INSERT INTO user_activity_logs (user_id, google_id, email, action, details, ip_address, user_agent, timestamp)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (user_id, google_id, email, action, activity_details_json, client_ip, user_agent, now_ts))
            else:
                c.execute("""
                    INSERT INTO user_activity_logs (user_id, google_id, email, action, details, ip_address, user_agent, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (user_id, google_id, email, action, activity_details_json, client_ip, user_agent, now_ts))
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error logging user activity: {e}")
def ensure_verse_id(c, db_type, verse_id, verse_payload=None):
    """Ensure a verse exists in DB and return a valid verse_id."""
    try:
        if verse_id:
            if db_type == 'postgres':
                c.execute("SELECT id FROM verses WHERE id = %s", (verse_id,))
            else:
                c.execute("SELECT id FROM verses WHERE id = ?", (verse_id,))
            row = c.fetchone()
            if row:
                return row['id'] if hasattr(row, 'keys') else row[0]
    except Exception:
        pass

    if not verse_payload:
        return verse_id

    ref = (verse_payload.get('reference') or verse_payload.get('ref') or '').strip()
    text = (verse_payload.get('text') or '').strip()
    if not ref or not text:
        return verse_id

    trans = (verse_payload.get('translation') or verse_payload.get('trans') or '').strip()
    source = (verse_payload.get('source') or '').strip()
    book = (verse_payload.get('book') or '').strip()
    now = datetime.now().isoformat()

    try:
        if db_type == 'postgres':
            c.execute("SELECT id FROM verses WHERE reference = %s AND text = %s", (ref, text))
        else:
            c.execute("SELECT id FROM verses WHERE reference = ? AND text = ?", (ref, text))
        row = c.fetchone()
        if row:
            return row['id'] if hasattr(row, 'keys') else row[0]
    except Exception:
        pass

    try:
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO verses (reference, text, translation, source, timestamp, book)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (ref, text, trans, source, now, book))
            c.execute("SELECT id FROM verses WHERE reference = %s AND text = %s", (ref, text))
        else:
            c.execute("""
                INSERT INTO verses (reference, text, translation, source, timestamp, book)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (ref, text, trans, source, now, book))
            c.execute("SELECT id FROM verses WHERE reference = ? AND text = ?", (ref, text))
        row = c.fetchone()
        if row:
            return row['id'] if hasattr(row, 'keys') else row[0]
    except Exception:
        pass
    return verse_id

@app.route('/api/generate-recommendation', methods=['POST'])
def generate_rec():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned"}), 403
    
    payload = request.get_json(silent=True) or {}
    exclude_ids = payload.get('exclude_ids') if isinstance(payload, dict) else None
    rec = generator.generate_smart_recommendation(session['user_id'], exclude_ids=exclude_ids)
    if rec:
        return jsonify({"success": True, "recommendation": rec})
    return jsonify({"success": False})

@app.route('/api/comments/<int:verse_id>')
def get_comments(verse_id):
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        ensure_comment_social_tables(c, db_type)
        conn.commit()

        if db_type == 'postgres':
            c.execute("""
                SELECT
                    cm.id, cm.user_id, cm.text, cm.timestamp, cm.google_name, cm.google_picture,
                    u.name, COALESCE(u.custom_picture, u.picture) AS picture, u.role, u.avatar_decoration
                FROM comments cm
                LEFT JOIN users u ON cm.user_id = u.id
                WHERE cm.verse_id = %s
                  AND COALESCE(cm.is_deleted, 0) = 0
                ORDER BY cm.timestamp DESC
            """, (verse_id,))
        else:
            c.execute("""
                SELECT
                    cm.id, cm.user_id, cm.text, cm.timestamp, cm.google_name, cm.google_picture,
                    u.name, COALESCE(u.custom_picture, u.picture) AS picture, u.role, u.avatar_decoration
                FROM comments cm
                LEFT JOIN users u ON cm.user_id = u.id
                WHERE cm.verse_id = ?
                  AND COALESCE(cm.is_deleted, 0) = 0
                ORDER BY cm.timestamp DESC
            """, (verse_id,))

        rows = c.fetchall()
        if not rows:
            return jsonify([])

        prepared = []
        comment_ids = []
        equipped_cache = {}
        for row in rows:
            try:
                comment_id = row['id']
                user_id = row['user_id']
                text = row['text']
                timestamp = row['timestamp']
                google_name = row['google_name']
                google_picture = row['google_picture']
                db_name = row['name']
                db_picture = row['picture']
                db_role = row['role']
                db_decor = row_value(row, 'avatar_decoration') or ""
            except Exception:
                comment_id = row[0]
                user_id = row[1]
                text = row[2]
                timestamp = row[3]
                google_name = row[4]
                google_picture = row[5]
                db_name = row[6] if len(row) > 6 else None
                db_picture = row[7] if len(row) > 7 else None
                db_role = row[8] if len(row) > 8 else None
                db_decor = row[9] if len(row) > 9 else ""

            prepared.append({
                "id": int(comment_id),
                "user_id": user_id,
                "text": text,
                "timestamp": timestamp,
                "user_name": db_name or google_name or "Anonymous",
                "user_picture": db_picture or google_picture or "",
                "avatar_decoration": db_decor or "",
                "user_role": normalize_role(db_role or "user")
            })
            comment_ids.append(int(comment_id))

        reactions_map = get_reaction_counts_bulk(c, db_type, "comment", comment_ids)
        replies_map = get_replies_for_parents(c, db_type, "comment", comment_ids, equipped_cache=equipped_cache)

        comments = []
        for item in prepared:
            uid = item["user_id"]
            cache_key = int(uid) if uid is not None else None
            if cache_key is not None and cache_key in equipped_cache:
                equipped = equipped_cache[cache_key]
            else:
                equipped = get_user_equipped_items(c, db_type, uid)
                if cache_key is not None:
                    equipped_cache[cache_key] = equipped

            replies = replies_map.get(item["id"], [])
            comments.append({
                "id": item["id"],
                "text": item["text"] or "",
                "timestamp": item["timestamp"],
                "user_name": item["user_name"],
                "user_picture": item["user_picture"],
                "avatar_decoration": item["avatar_decoration"],
                "user_id": uid,
                "user_role": item["user_role"],
                "reactions": reactions_map.get(item["id"], {"heart": 0, "pray": 0, "cross": 0}),
                "replies": replies,
                "reply_count": len(replies),
                "equipped_frame": equipped["frame"],
                "equipped_name_color": equipped["name_color"],
                "equipped_title": equipped["title"],
                "equipped_badges": equipped["badges"],
                "equipped_chat_effect": equipped["chat_effect"]
            })

        return jsonify(comments)
    except Exception as e:
        logger.error(f"Get comments error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

def check_comment_restriction(user_id):
    """Check if user is restricted from commenting. Returns (is_restricted, reason, expires_at)"""
    global RESTRICTION_SCHEMA_READY
    try:
        conn, db_type = get_db()
        c = conn.cursor()
        
        if not RESTRICTION_SCHEMA_READY:
            # Ensure table exists with appropriate syntax (once per process)
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
            conn.commit()
            RESTRICTION_SCHEMA_READY = True
        
        # Check for active restriction
        now = datetime.now().isoformat()
        if db_type == 'postgres':
            c.execute("SELECT reason, expires_at FROM comment_restrictions WHERE user_id = %s AND expires_at > %s",
                     (user_id, now))
        else:
            c.execute("SELECT reason, expires_at FROM comment_restrictions WHERE user_id = ? AND expires_at > ?",
                     (user_id, now))
        row = c.fetchone()
        conn.close()
        if row:
            return (True, row[0], row[1])
        return (False, None, None)
    except Exception as e:
        logger.error(f"Check restriction error: {e}")
        return (False, None, None)

@app.route('/api/comments', methods=['POST'])
def post_comment():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned", "message": "Account banned"}), 403
    
    # Check for comment restriction
    is_restricted, reason, expires_at = check_comment_restriction(session['user_id'])
    if is_restricted:
        expires_str = datetime.fromisoformat(expires_at).strftime("%Y-%m-%d %H:%M") if expires_at else "soon"
        return jsonify({
            "error": "restricted", 
            "message": f"You have been restricted from commenting due to {reason} for 1-24hrs",
            "reason": reason,
            "expires_at": expires_str
        }), 403
    
    data = request.get_json()
    verse_id = data.get('verse_id')
    text = data.get('text', '').strip()
    
    if not text:
        return jsonify({"error": "Empty comment"}), 400
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        if db_type == 'postgres':
            c.execute("INSERT INTO comments (user_id, verse_id, text, timestamp, google_name, google_picture) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                      (session['user_id'], verse_id, text, datetime.now().isoformat(), 
                       session.get('user_name'), session.get('user_picture')))
            result = c.fetchone()
            comment_id = result['id'] if result else None
        else:
            c.execute("INSERT INTO comments (user_id, verse_id, text, timestamp, google_name, google_picture) VALUES (?, ?, ?, ?, ?, ?)",
                      (session['user_id'], verse_id, text, datetime.now().isoformat(), 
                       session.get('user_name'), session.get('user_picture')))
            comment_id = c.lastrowid
        
        conn.commit()
        if comment_id:
            record_daily_action(session['user_id'], 'comment', comment_id)
            log_user_activity(
                "USER_COMMENT",
                user_id=session['user_id'],
                message="Posted a comment",
                extras={"comment_id": comment_id, "verse_id": verse_id}
            )
        return jsonify({"success": True, "id": comment_id})
    except Exception as e:
        logger.error(f"Post comment error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/community')
def get_community_messages():
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        room_slug = (request.args.get('room') or '').strip().lower()
        if room_slug and room_slug != 'general':
            ensure_research_feature_tables(c, db_type)
            conn.commit()
            c.execute("""
                SELECT m.id, m.user_id, m.text, m.timestamp, m.google_name, m.google_picture,
                       u.name, COALESCE(u.custom_picture, u.picture) AS picture, u.role, u.avatar_decoration
                FROM research_community_messages m
                LEFT JOIN users u ON m.user_id = u.id
                WHERE m.room_slug = ?
                ORDER BY m.timestamp DESC
                LIMIT 100
            """ if db_type != 'postgres' else """
                SELECT m.id, m.user_id, m.text, m.timestamp, m.google_name, m.google_picture,
                       u.name, COALESCE(u.custom_picture, u.picture) AS picture, u.role, u.avatar_decoration
                FROM research_community_messages m
                LEFT JOIN users u ON m.user_id = u.id
                WHERE m.room_slug = %s
                ORDER BY m.timestamp DESC
                LIMIT 100
            """, (room_slug,))
            rows = c.fetchall()
            payload = []
            for row in rows:
                payload.append({
                    "id": row_pick(row, 'id', 0),
                    "text": row_pick(row, 'text', 2) or "",
                    "timestamp": row_pick(row, 'timestamp', 3),
                    "user_name": row_pick(row, 'name', 6) or row_pick(row, 'google_name', 4) or "Anonymous",
                    "user_picture": row_pick(row, 'picture', 7) or row_pick(row, 'google_picture', 5) or "",
                    "avatar_decoration": row_pick(row, 'avatar_decoration', 9) or "",
                    "user_id": row_pick(row, 'user_id', 1),
                    "user_role": row_pick(row, 'role', 8) or "user",
                    "reactions": {},
                    "replies": [],
                    "reply_count": 0
                })
            return jsonify(payload)

        ensure_comment_social_tables(c, db_type)
        conn.commit()

        if db_type == 'postgres':
            c.execute("""
                SELECT
                    cm.id, cm.user_id, cm.text, cm.timestamp, cm.google_name, cm.google_picture,
                    u.name, COALESCE(u.custom_picture, u.picture) AS picture, u.role, u.avatar_decoration
                FROM community_messages cm
                LEFT JOIN users u ON cm.user_id = u.id
                ORDER BY timestamp DESC
                LIMIT 100
            """)
        else:
            c.execute("""
                SELECT
                    cm.id, cm.user_id, cm.text, cm.timestamp, cm.google_name, cm.google_picture,
                    u.name, COALESCE(u.custom_picture, u.picture) AS picture, u.role, u.avatar_decoration
                FROM community_messages cm
                LEFT JOIN users u ON cm.user_id = u.id
                ORDER BY timestamp DESC
                LIMIT 100
            """)
        
        rows = c.fetchall()
        if not rows:
            return jsonify([])

        prepared = []
        msg_ids = []
        equipped_cache = {}
        for row in rows:
            try:
                msg_id = row['id']
                user_id = row['user_id']
                text = row['text']
                timestamp = row['timestamp']
                google_name = row['google_name']
                google_picture = row['google_picture']
                db_name = row['name']
                db_picture = row['picture']
                db_role = row['role']
                db_decor = row_value(row, 'avatar_decoration') or ""
            except (TypeError, KeyError):
                msg_id = row[0]
                user_id = row[1]
                text = row[2]
                timestamp = row[3]
                google_name = row[4]
                google_picture = row[5]
                db_name = row[6] if len(row) > 6 else None
                db_picture = row[7] if len(row) > 7 else None
                db_role = row[8] if len(row) > 8 else None
                db_decor = row[9] if len(row) > 9 else ""

            prepared.append({
                "id": int(msg_id),
                "user_id": user_id,
                "text": text,
                "timestamp": timestamp,
                "user_name": db_name or google_name or "Anonymous",
                "user_picture": db_picture or google_picture or "",
                "avatar_decoration": db_decor or "",
                "user_role": normalize_role(db_role or "user")
            })
            msg_ids.append(int(msg_id))

        reactions_map = get_reaction_counts_bulk(c, db_type, "community", msg_ids)
        replies_map = get_replies_for_parents(c, db_type, "community", msg_ids, equipped_cache=equipped_cache)

        messages = []
        for item in prepared:
            uid = item["user_id"]
            cache_key = int(uid) if uid is not None else None
            if cache_key is not None and cache_key in equipped_cache:
                equipped = equipped_cache[cache_key]
            else:
                equipped = get_user_equipped_items(c, db_type, uid)
                if cache_key is not None:
                    equipped_cache[cache_key] = equipped

            replies = replies_map.get(item["id"], [])
            messages.append({
                "id": item["id"],
                "text": item["text"] or "",
                "timestamp": item["timestamp"],
                "user_name": item["user_name"],
                "user_picture": item["user_picture"],
                "avatar_decoration": item["avatar_decoration"],
                "user_id": uid,
                "user_role": item["user_role"],
                "reactions": reactions_map.get(item["id"], {"heart": 0, "pray": 0, "cross": 0}),
                "replies": replies,
                "reply_count": len(replies),
                "equipped_frame": equipped["frame"],
                "equipped_name_color": equipped["name_color"],
                "equipped_title": equipped["title"],
                "equipped_badges": equipped["badges"],
                "equipped_chat_effect": equipped["chat_effect"]
            })

        return jsonify(messages)
    except Exception as e:
        logger.error(f"Get community error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/community', methods=['POST'])
def post_community_message():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned", "message": "Account banned"}), 403
    
    # Check for comment restriction
    is_restricted, reason, expires_at = check_comment_restriction(session['user_id'])
    if is_restricted:
        expires_str = datetime.fromisoformat(expires_at).strftime("%Y-%m-%d %H:%M") if expires_at else "soon"
        return jsonify({
            "error": "restricted", 
            "message": f"You have been restricted from commenting due to {reason} for 1-24hrs",
            "reason": reason,
            "expires_at": expires_str
        }), 403
    
    data = request.get_json()
    text = data.get('text', '').strip()
    
    if not text:
        return jsonify({"error": "Empty message"}), 400
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        room_slug = (data.get('room') or '').strip().lower()
        if room_slug and room_slug != 'general':
            ensure_research_feature_tables(c, db_type)
            conn.commit()
            c.execute("""
                INSERT INTO research_community_messages (room_slug, user_id, text, timestamp, google_name, google_picture)
                VALUES (?, ?, ?, ?, ?, ?)
            """ if db_type != 'postgres' else """
                INSERT INTO research_community_messages (room_slug, user_id, text, timestamp, google_name, google_picture)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (room_slug, session['user_id'], text, datetime.now().isoformat(), session.get('user_name'), session.get('user_picture')))
            conn.commit()
            return jsonify({"success": True})

        if db_type == 'postgres':
            c.execute("INSERT INTO community_messages (user_id, text, timestamp, google_name, google_picture) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                      (session['user_id'], text, datetime.now().isoformat(), 
                       session.get('user_name'), session.get('user_picture')))
            result = c.fetchone()
            message_id = result['id'] if result else None
        else:
            c.execute("INSERT INTO community_messages (user_id, text, timestamp, google_name, google_picture) VALUES (?, ?, ?, ?, ?)",
                      (session['user_id'], text, datetime.now().isoformat(), 
                       session.get('user_name'), session.get('user_picture')))
            message_id = c.lastrowid
        
        conn.commit()
        if message_id:
            record_daily_action(session['user_id'], 'comment', message_id)
            log_user_activity(
                "USER_COMMUNITY",
                user_id=session['user_id'],
                message="Posted a community message",
                extras={"message_id": message_id}
            )
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Post community error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/users/search')
def search_users():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify([])
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        token = f"%{q}%"
        if db_type == 'postgres':
            c.execute("""
                SELECT id, name, email, COALESCE(custom_picture, picture) AS picture, role, avatar_decoration
                FROM users
                WHERE id <> %s AND (
                    COALESCE(name,'') ILIKE %s
                    OR COALESCE(email,'') ILIKE %s
                    OR CAST(id AS TEXT) ILIKE %s
                )
                ORDER BY id DESC
                LIMIT 10
            """, (session['user_id'], token, token, token))
        else:
            c.execute("""
                SELECT id, name, email, COALESCE(custom_picture, picture) AS picture, role, avatar_decoration
                FROM users
                WHERE id <> ? AND (
                    LOWER(COALESCE(name,'')) LIKE LOWER(?)
                    OR LOWER(COALESCE(email,'')) LIKE LOWER(?)
                    OR CAST(id AS TEXT) LIKE ?
                )
                ORDER BY id DESC
                LIMIT 10
            """, (session['user_id'], token, token, token))
        rows = c.fetchall()
        results = []
        for row in rows:
            try:
                results.append({
                    "id": row['id'],
                    "name": row['name'] or "User",
                    "email": row_value(row, 'email') or "",
                    "picture": row['picture'] or "",
                    "role": normalize_role(row['role'] or 'user'),
                    "avatar_decoration": row_value(row, 'avatar_decoration') or ""
                })
            except Exception:
                results.append({
                    "id": row[0],
                    "name": row[1] or "User",
                    "email": row[2] or "",
                    "picture": row[3] or "",
                    "role": normalize_role(row[4] if len(row) > 4 else 'user'),
                    "avatar_decoration": row[5] if len(row) > 5 else ""
                })
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/users/recent')
def recent_users():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        limit = max(1, min(12, int(request.args.get('limit', 8))))
        uid = session['user_id']
        if db_type == 'postgres':
            c.execute(f"""
                SELECT u.id, u.name, COALESCE(u.custom_picture, u.picture) AS picture, u.role, u.avatar_decoration
                FROM users u
                WHERE u.id <> %s AND u.id IN (
                    SELECT user_id FROM comments ORDER BY timestamp DESC LIMIT {limit * 5}
                    UNION
                    SELECT user_id FROM community_messages ORDER BY timestamp DESC LIMIT {limit * 5}
                )
                ORDER BY u.id DESC
                LIMIT {limit}
            """, (uid,))
        else:
            c.execute(f"""
                SELECT u.id, u.name, COALESCE(u.custom_picture, u.picture) AS picture, u.role, u.avatar_decoration
                FROM users u
                WHERE u.id <> ? AND u.id IN (
                    SELECT user_id FROM comments ORDER BY timestamp DESC LIMIT {limit * 5}
                    UNION
                    SELECT user_id FROM community_messages ORDER BY timestamp DESC LIMIT {limit * 5}
                )
                ORDER BY u.id DESC
                LIMIT {limit}
            """, (uid,))
        rows = c.fetchall()
        results = []
        for row in rows:
            try:
                results.append({
                    "id": row['id'],
                    "name": row['name'] or "User",
                    "picture": row['picture'] or "",
                    "role": normalize_role(row['role'] or 'user'),
                    "avatar_decoration": row_value(row, 'avatar_decoration') or ""
                })
            except Exception:
                results.append({
                    "id": row[0],
                    "name": row[1] or "User",
                    "picture": row[2] or "",
                    "role": normalize_role(row[3] if len(row) > 3 else 'user'),
                    "avatar_decoration": row[4] if len(row) > 4 else ""
                })
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/dm/threads')
def dm_threads():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned"}), 403
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        ensure_dm_tables(c, db_type)
        conn.commit()
        uid = session['user_id']
        if db_type == 'postgres':
            c.execute("""
                WITH user_threads AS (
                    SELECT
                        id,
                        CASE WHEN sender_id = %s THEN recipient_id ELSE sender_id END AS other_id,
                        message,
                        created_at,
                        sender_id
                    FROM direct_messages
                    WHERE sender_id = %s OR recipient_id = %s
                ),
                last_msg AS (
                    SELECT DISTINCT ON (other_id)
                        other_id, message, created_at, sender_id
                    FROM user_threads
                    ORDER BY other_id, created_at DESC, id DESC
                ),
                unread AS (
                    SELECT sender_id AS other_id, COUNT(*)::int AS unread
                    FROM direct_messages
                    WHERE recipient_id = %s AND COALESCE(is_read, 0) = 0
                    GROUP BY sender_id
                )
                SELECT
                    lm.other_id AS other_id,
                    COALESCE(u.name, 'User') AS name,
                    COALESCE(u.custom_picture, u.picture, '') AS picture,
                    COALESCE(u.role, 'user') AS role,
                    COALESCE(u.avatar_decoration, '') AS avatar_decoration,
                    lm.message AS last_message,
                    lm.created_at AS last_at,
                    lm.sender_id AS last_sender,
                    COALESCE(unread.unread, 0) AS unread
                FROM last_msg lm
                LEFT JOIN users u ON u.id = lm.other_id
                LEFT JOIN unread ON unread.other_id = lm.other_id
                ORDER BY lm.created_at DESC NULLS LAST, other_id DESC
            """, (uid, uid, uid, uid))
        else:
            c.execute("""
                WITH user_threads AS (
                    SELECT
                        id,
                        CASE WHEN sender_id = ? THEN recipient_id ELSE sender_id END AS other_id,
                        message,
                        created_at,
                        sender_id
                    FROM direct_messages
                    WHERE sender_id = ? OR recipient_id = ?
                ),
                ranked AS (
                    SELECT
                        other_id, message, created_at, sender_id,
                        ROW_NUMBER() OVER (PARTITION BY other_id ORDER BY created_at DESC, id DESC) AS rn
                    FROM user_threads
                ),
                unread AS (
                    SELECT sender_id AS other_id, COUNT(*) AS unread
                    FROM direct_messages
                    WHERE recipient_id = ? AND COALESCE(is_read, 0) = 0
                    GROUP BY sender_id
                )
                SELECT
                    r.other_id AS other_id,
                    COALESCE(u.name, 'User') AS name,
                    COALESCE(u.custom_picture, u.picture, '') AS picture,
                    COALESCE(u.role, 'user') AS role,
                    COALESCE(u.avatar_decoration, '') AS avatar_decoration,
                    r.message AS last_message,
                    r.created_at AS last_at,
                    r.sender_id AS last_sender,
                    COALESCE(unread.unread, 0) AS unread
                FROM ranked r
                LEFT JOIN users u ON u.id = r.other_id
                LEFT JOIN unread ON unread.other_id = r.other_id
                WHERE r.rn = 1
                ORDER BY r.created_at DESC, r.other_id DESC
            """, (uid, uid, uid, uid))

        rows = c.fetchall()
        threads = []
        for row in rows:
            try:
                threads.append({
                    "user_id": row['other_id'],
                    "name": row['name'] or 'User',
                    "picture": row['picture'] or '',
                    "role": normalize_role(row['role'] or 'user'),
                    "avatar_decoration": row_value(row, 'avatar_decoration') or '',
                    "last_message": row_value(row, 'last_message') or '',
                    "last_at": row_value(row, 'last_at'),
                    "last_sender": row_value(row, 'last_sender'),
                    "unread": int(row_value(row, 'unread', 0) or 0)
                })
            except Exception:
                threads.append({
                    "user_id": row[0],
                    "name": (row[1] if len(row) > 1 else None) or 'User',
                    "picture": (row[2] if len(row) > 2 else None) or '',
                    "role": normalize_role(row[3] if len(row) > 3 else 'user'),
                    "avatar_decoration": row[4] if len(row) > 4 and row[4] else '',
                    "last_message": row[5] if len(row) > 5 and row[5] else '',
                    "last_at": row[6] if len(row) > 6 else None,
                    "last_sender": row[7] if len(row) > 7 else None,
                    "unread": int(row[8] if len(row) > 8 and row[8] is not None else 0)
                })
        return jsonify(threads)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/dm/messages/<int:other_id>')
def dm_messages(other_id):
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned"}), 403
    if other_id == session['user_id']:
        return jsonify({"error": "Invalid target"}), 400
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        ensure_dm_tables(c, db_type)
        conn.commit()
        uid = session['user_id']
        if db_type == 'postgres':
            c.execute("""
                SELECT id, sender_id, recipient_id, message, created_at, is_read
                FROM direct_messages
                WHERE (sender_id = %s AND recipient_id = %s) OR (sender_id = %s AND recipient_id = %s)
                ORDER BY created_at ASC, id ASC
                LIMIT 200
            """, (uid, other_id, other_id, uid))
        else:
            c.execute("""
                SELECT id, sender_id, recipient_id, message, created_at, is_read
                FROM direct_messages
                WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?)
                ORDER BY created_at ASC, id ASC
                LIMIT 200
            """, (uid, other_id, other_id, uid))
        rows = c.fetchall()
        messages = []
        for row in rows:
            try:
                messages.append({
                    "id": row['id'],
                    "sender_id": row['sender_id'],
                    "recipient_id": row['recipient_id'],
                    "message": row['message'],
                    "created_at": row['created_at'],
                    "is_read": row['is_read']
                })
            except Exception:
                messages.append({
                    "id": row[0],
                    "sender_id": row[1],
                    "recipient_id": row[2],
                    "message": row[3],
                    "created_at": row[4],
                    "is_read": row[5] if len(row) > 5 else 0
                })
        # mark read
        if db_type == 'postgres':
            c.execute("UPDATE direct_messages SET is_read = 1 WHERE sender_id = %s AND recipient_id = %s AND COALESCE(is_read, 0) = 0", (other_id, uid))
        else:
            c.execute("UPDATE direct_messages SET is_read = 1 WHERE sender_id = ? AND recipient_id = ? AND COALESCE(is_read, 0) = 0", (other_id, uid))
        conn.commit()
        return jsonify(messages)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/dm/send', methods=['POST'])
def dm_send():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned"}), 403
    is_restricted, reason, expires_at = check_comment_restriction(session['user_id'])
    if is_restricted:
        expires_str = datetime.fromisoformat(expires_at).strftime("%Y-%m-%d %H:%M") if expires_at else "soon"
        return jsonify({
            "error": "restricted",
            "message": f"You have been restricted from chatting due to {reason}",
            "reason": reason,
            "expires_at": expires_str
        }), 403
    data = request.get_json() or {}
    recipient_id = int(data.get('recipient_id') or 0)
    message = (data.get('message') or '').strip()
    if not recipient_id or recipient_id == session['user_id']:
        return jsonify({"error": "Invalid recipient"}), 400
    if not message:
        return jsonify({"error": "Empty message"}), 400
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        ensure_dm_tables(c, db_type)
        conn.commit()
        now = datetime.now().isoformat()
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO direct_messages (sender_id, recipient_id, message, created_at, is_read)
                VALUES (%s, %s, %s, %s, 0)
            """, (session['user_id'], recipient_id, message, now))
        else:
            c.execute("""
                INSERT INTO direct_messages (sender_id, recipient_id, message, created_at, is_read)
                VALUES (?, ?, ?, ?, 0)
            """, (session['user_id'], recipient_id, message, now))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/dm/thread/<int:other_id>/delete', methods=['POST'])
def dm_delete_thread(other_id):
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned"}), 403
    if other_id == session['user_id']:
        return jsonify({"error": "Invalid target"}), 400
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        ensure_dm_tables(c, db_type)
        conn.commit()
        uid = session['user_id']
        if db_type == 'postgres':
            c.execute("""
                DELETE FROM direct_messages
                WHERE (sender_id = %s AND recipient_id = %s)
                   OR (sender_id = %s AND recipient_id = %s)
            """, (uid, other_id, other_id, uid))
            c.execute("DELETE FROM dm_typing WHERE (user_id = %s AND other_id = %s) OR (user_id = %s AND other_id = %s)", (uid, other_id, other_id, uid))
        else:
            c.execute("""
                DELETE FROM direct_messages
                WHERE (sender_id = ? AND recipient_id = ?)
                   OR (sender_id = ? AND recipient_id = ?)
            """, (uid, other_id, other_id, uid))
            c.execute("DELETE FROM dm_typing WHERE (user_id = ? AND other_id = ?) OR (user_id = ? AND other_id = ?)", (uid, other_id, other_id, uid))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/dm/typing', methods=['POST'])
def dm_typing():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    data = request.get_json() or {}
    other_id = int(data.get('other_id') or 0)
    if not other_id or other_id == session['user_id']:
        return jsonify({"error": "Invalid target"}), 400
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        ensure_dm_tables(c, db_type)
        conn.commit()
        now = datetime.now().isoformat()
        uid = session['user_id']
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO dm_typing (user_id, other_id, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, other_id) DO UPDATE SET updated_at = EXCLUDED.updated_at
            """, (uid, other_id, now))
        else:
            c.execute("""
                INSERT OR REPLACE INTO dm_typing (user_id, other_id, updated_at)
                VALUES (?, ?, ?)
            """, (uid, other_id, now))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/dm/typing/<int:other_id>')
def dm_typing_status(other_id):
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    if other_id == session['user_id']:
        return jsonify({"typing": False})
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        ensure_dm_tables(c, db_type)
        conn.commit()
        uid = session['user_id']
        if db_type == 'postgres':
            c.execute("SELECT updated_at FROM dm_typing WHERE user_id = %s AND other_id = %s", (other_id, uid))
        else:
            c.execute("SELECT updated_at FROM dm_typing WHERE user_id = ? AND other_id = ?", (other_id, uid))
        row = c.fetchone()
        if not row:
            return jsonify({"typing": False})
        try:
            ts = row['updated_at']
        except Exception:
            ts = row[0]
        try:
            ts_dt = datetime.fromisoformat(str(ts).replace('Z', ''))
        except Exception:
            return jsonify({"typing": False})
        delta = (datetime.now() - ts_dt).total_seconds()
        return jsonify({"typing": delta <= 6})
    except Exception:
        return jsonify({"typing": False})
    finally:
        conn.close()

@app.route('/api/comments/reaction', methods=['POST'])
def add_comment_reaction():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json() or {}
    item_id = data.get('item_id')
    item_type = str(data.get('item_type', 'comment')).strip().lower()
    reaction = str(data.get('reaction', '')).strip().lower()

    if item_type not in ('comment', 'community'):
        return jsonify({"error": "Invalid item_type"}), 400
    if reaction not in ('heart', 'pray', 'cross'):
        return jsonify({"error": "Invalid reaction"}), 400
    if not item_id:
        return jsonify({"error": "item_id required"}), 400

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        ensure_comment_social_tables(c, db_type)
        now = datetime.now().isoformat()

        if db_type == 'postgres':
            c.execute("""
                SELECT id FROM comment_reactions
                WHERE item_type = %s AND item_id = %s AND user_id = %s AND reaction = %s
            """, (item_type, item_id, session['user_id'], reaction))
        else:
            c.execute("""
                SELECT id FROM comment_reactions
                WHERE item_type = ? AND item_id = ? AND user_id = ? AND reaction = ?
            """, (item_type, item_id, session['user_id'], reaction))
        exists = c.fetchone()

        if exists:
            if db_type == 'postgres':
                c.execute("""
                    DELETE FROM comment_reactions
                    WHERE item_type = %s AND item_id = %s AND user_id = %s AND reaction = %s
                """, (item_type, item_id, session['user_id'], reaction))
            else:
                c.execute("""
                    DELETE FROM comment_reactions
                    WHERE item_type = ? AND item_id = ? AND user_id = ? AND reaction = ?
                """, (item_type, item_id, session['user_id'], reaction))
            active = False
        else:
            if db_type == 'postgres':
                c.execute("""
                    INSERT INTO comment_reactions (item_type, item_id, user_id, reaction, timestamp)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (item_type, item_id, user_id, reaction) DO NOTHING
                """, (item_type, item_id, session['user_id'], reaction, now))
            else:
                c.execute("""
                    INSERT OR IGNORE INTO comment_reactions (item_type, item_id, user_id, reaction, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                """, (item_type, item_id, session['user_id'], reaction, now))
            active = True

        conn.commit()
        counts = get_reaction_counts(c, db_type, item_type, int(item_id))
        return jsonify({"success": True, "active": active, "reactions": counts})
    except Exception as e:
        logger.error(f"Add reaction error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/comments/replies', methods=['POST'])
def post_comment_reply():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json() or {}
    parent_type = str(data.get('parent_type', 'comment')).strip().lower()
    parent_id = data.get('parent_id')
    text = str(data.get('text', '')).strip()

    if parent_type not in ('comment', 'community'):
        return jsonify({"error": "Invalid parent_type"}), 400
    if not parent_id:
        return jsonify({"error": "parent_id required"}), 400
    if not text:
        return jsonify({"error": "Empty reply"}), 400

    is_banned, _, _ = check_ban_status(session['user_id'])
    if is_banned:
        return jsonify({"error": "banned", "message": "Account banned"}), 403
    is_restricted, reason, expires_at = check_comment_restriction(session['user_id'])
    if is_restricted:
        expires_str = datetime.fromisoformat(expires_at).strftime("%Y-%m-%d %H:%M") if expires_at else "soon"
        return jsonify({
            "error": "restricted",
            "message": f"You have been restricted from commenting due to {reason} for 1-24hrs",
            "reason": reason,
            "expires_at": expires_str
        }), 403

    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    try:
        ensure_comment_social_tables(c, db_type)
        now = datetime.now().isoformat()
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO comment_replies (parent_type, parent_id, user_id, text, timestamp, google_name, google_picture)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (parent_type, parent_id, session['user_id'], text, now, session.get('user_name'), session.get('user_picture')))
        else:
            c.execute("""
                INSERT INTO comment_replies (parent_type, parent_id, user_id, text, timestamp, google_name, google_picture)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (parent_type, parent_id, session['user_id'], text, now, session.get('user_name'), session.get('user_picture')))
        conn.commit()
        replies = get_replies_for_parent(c, db_type, parent_type, int(parent_id))
        return jsonify({"success": True, "replies": replies, "reply_count": len(replies)})
    except Exception as e:
        logger.error(f"Post reply error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/check_like/<int:verse_id>')
def check_like(verse_id):
    if 'user_id' not in session:
        return jsonify({"liked": False})
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        if db_type == 'postgres':
            c.execute("SELECT id FROM likes WHERE user_id = %s AND verse_id = %s", (session['user_id'], verse_id))
        else:
            c.execute("SELECT id FROM likes WHERE user_id = ? AND verse_id = ?", (session['user_id'], verse_id))
        
        liked = c.fetchone() is not None
        return jsonify({"liked": liked})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/check_save/<int:verse_id>')
def check_save(verse_id):
    if 'user_id' not in session:
        return jsonify({"saved": False})
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        if db_type == 'postgres':
            c.execute("SELECT id FROM saves WHERE user_id = %s AND verse_id = %s", (session['user_id'], verse_id))
        else:
            c.execute("SELECT id FROM saves WHERE user_id = ? AND verse_id = ?", (session['user_id'], verse_id))
        
        saved = c.fetchone() is not None
        return jsonify({"saved": saved})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# Admin delete comment/community endpoints (needed for frontend)
@app.route('/api/admin/delete_comment/<int:comment_id>', methods=['DELETE'])
def delete_comment_api(comment_id):
    """Delete a comment (admin only)"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    # Check if user is admin
    if not session.get('is_admin'):
        return jsonify({"error": "Admin required"}), 403
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        # Soft delete by setting is_deleted = 1
        if db_type == 'postgres':
            c.execute("ALTER TABLE comment_replies ADD COLUMN IF NOT EXISTS is_deleted INTEGER DEFAULT 0")
            c.execute("UPDATE comments SET is_deleted = 1 WHERE id = %s", (comment_id,))
            c.execute(
                "UPDATE comment_replies SET is_deleted = 1 WHERE parent_type = %s AND parent_id = %s",
                ('comment', comment_id)
            )
        else:
            try:
                c.execute("SELECT is_deleted FROM comment_replies LIMIT 1")
            except Exception:
                c.execute("ALTER TABLE comment_replies ADD COLUMN is_deleted INTEGER DEFAULT 0")
            c.execute("UPDATE comments SET is_deleted = 1 WHERE id = ?", (comment_id,))
            c.execute(
                "UPDATE comment_replies SET is_deleted = 1 WHERE parent_type = ? AND parent_id = ?",
                ('comment', comment_id)
            )
        conn.commit()
        
        # Log the action
        log_action(session.get('user_id'), 'DELETE_COMMENT', comment_id, {'type': 'comment'})
        
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Delete comment error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/admin/delete_community/<int:message_id>', methods=['DELETE'])
def delete_community_api(message_id):
    """Delete a community message (admin only)"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    # Check if user is admin
    if not session.get('is_admin'):
        return jsonify({"error": "Admin required"}), 403
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        # Hard delete community messages (no is_deleted column)
        if db_type == 'postgres':
            c.execute("ALTER TABLE comment_replies ADD COLUMN IF NOT EXISTS is_deleted INTEGER DEFAULT 0")
            c.execute("DELETE FROM community_messages WHERE id = %s", (message_id,))
            c.execute(
                "UPDATE comment_replies SET is_deleted = 1 WHERE parent_type = %s AND parent_id = %s",
                ('community', message_id)
            )
        else:
            try:
                c.execute("SELECT is_deleted FROM comment_replies LIMIT 1")
            except Exception:
                c.execute("ALTER TABLE comment_replies ADD COLUMN is_deleted INTEGER DEFAULT 0")
            c.execute("DELETE FROM community_messages WHERE id = ?", (message_id,))
            c.execute(
                "UPDATE comment_replies SET is_deleted = 1 WHERE parent_type = ? AND parent_id = ?",
                ('community', message_id)
            )
        conn.commit()
        
        # Log the action
        log_action(session.get('user_id'), 'DELETE_COMMENT', message_id, {'type': 'community'})
        
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Delete community message error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/liked_verses')
def get_liked_verses():
    """Get all verses the user has liked with book info"""
    if 'user_id' not in session:
        return jsonify([]), 401
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        if db_type == 'postgres':
            c.execute("""
                SELECT v.id, v.reference, v.book 
                FROM verses v 
                JOIN likes l ON v.id = l.verse_id 
                WHERE l.user_id = %s
                ORDER BY l.timestamp DESC
            """, (session['user_id'],))
        else:
            c.execute("""
                SELECT v.id, v.reference, v.book 
                FROM verses v 
                JOIN likes l ON v.id = l.verse_id 
                WHERE l.user_id = ?
                ORDER BY l.timestamp DESC
            """, (session['user_id'],))
        
        rows = c.fetchall()
        verses = []
        for row in rows:
            try:
                verses.append({
                    "id": row['id'],
                    "ref": row['reference'],
                    "book": row['book']
                })
            except (TypeError, KeyError):
                verses.append({
                    "id": row[0],
                    "ref": row[1],
                    "book": row[2]
                })
        return jsonify(verses)
    except Exception as e:
        logger.error(f"Liked verses error: {e}")
        return jsonify([]), 500
    finally:
        conn.close()


@app.route('/api/saved_verses')
def get_saved_verses():
    """Get all verses the user has saved with book info"""
    if 'user_id' not in session:
        return jsonify([]), 401
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        if db_type == 'postgres':
            c.execute("""
                SELECT v.id, v.reference, v.book 
                FROM verses v 
                JOIN saves s ON v.id = s.verse_id 
                WHERE s.user_id = %s
                ORDER BY s.timestamp DESC
            """, (session['user_id'],))
        else:
            c.execute("""
                SELECT v.id, v.reference, v.book 
                FROM verses v 
                JOIN saves s ON v.id = s.verse_id 
                WHERE s.user_id = ?
                ORDER BY s.timestamp DESC
            """, (session['user_id'],))
        
        rows = c.fetchall()
        verses = []
        for row in rows:
            try:
                verses.append({
                    "id": row['id'],
                    "ref": row['reference'],
                    "book": row['book']
                })
            except (TypeError, KeyError):
                verses.append({
                    "id": row[0],
                    "ref": row[1],
                    "book": row[2]
                })
        return jsonify(verses)
    except Exception as e:
        logger.error(f"Saved verses error: {e}")
        return jsonify([]), 500
    finally:
        conn.close()

@app.route('/api/presence/ping', methods=['POST'])
def presence_ping():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    conn = None
    try:
        conn, db_type = get_db()
        c = get_cursor(conn, db_type)
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_presence (
                user_id INTEGER PRIMARY KEY,
                last_seen TEXT,
                last_path TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        now_iso = datetime.now().isoformat()
        path = request.json.get('path') if request.is_json else request.path
        if db_type == 'postgres':
            c.execute("""
                INSERT INTO user_presence (user_id, last_seen, last_path, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    last_seen = EXCLUDED.last_seen,
                    last_path = EXCLUDED.last_path,
                    updated_at = EXCLUDED.updated_at
            """, (session['user_id'], now_iso, path, now_iso))
        else:
            c.execute("""
                INSERT INTO user_presence (user_id, last_seen, last_path, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    last_path = excluded.last_path,
                    updated_at = excluded.updated_at
            """, (session['user_id'], now_iso, path, now_iso))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Presence ping error: {e}")
        if conn:
            try:
                conn.close()
            except:
                pass
        return jsonify({"error": str(e)}), 500

@app.route('/api/presence/online')
def presence_online():
    if 'user_id' not in session:
        return jsonify({"count": 0}), 401
    conn = None
    try:
        conn, db_type = get_db()
        c = get_cursor(conn, db_type)
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_presence (
                user_id INTEGER PRIMARY KEY,
                last_seen TEXT,
                last_path TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        if db_type == 'postgres':
            c.execute("SELECT user_id, last_seen FROM user_presence")
            rows = c.fetchall()
        else:
            c.execute("SELECT user_id, last_seen FROM user_presence")
            rows = c.fetchall()

        now = datetime.now()
        window = now - timedelta(minutes=3)
        count = 0
        for row in rows:
            try:
                last_seen = row['last_seen'] if isinstance(row, dict) or hasattr(row, 'keys') else row[1]
                if not last_seen:
                    continue
                last_dt = datetime.fromisoformat(str(last_seen))
                if last_dt >= window:
                    count += 1
            except Exception:
                continue
        conn.close()
        return jsonify({"count": count})
    except Exception as e:
        logger.error(f"Presence online error: {e}")
        if conn:
            try:
                conn.close()
            except:
                pass
        return jsonify({"count": 0}), 500

@app.route('/api/notifications')
def get_notifications():
    if 'user_id' not in session:
        return jsonify([]), 401
    conn = None
    try:
        conn, db_type = get_db()
        c = get_cursor(conn, db_type)
        if db_type == 'postgres':
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
        else:
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
        if db_type == 'postgres':
            c.execute("""
                SELECT id, title, message, notif_type, source, is_read, created_at
                FROM user_notifications
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 50
            """, (session['user_id'],))
        else:
            c.execute("""
                SELECT id, title, message, notif_type, source, is_read, created_at
                FROM user_notifications
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 50
            """, (session['user_id'],))
        rows = c.fetchall()
        conn.close()
        def _parse_ts(val):
            if not val:
                return None
            if isinstance(val, datetime):
                return val
            text = str(val).strip()
            if not text:
                return None
            try:
                return datetime.fromisoformat(text)
            except Exception:
                try:
                    return datetime.fromisoformat(text.replace(' ', 'T'))
                except Exception:
                    return None

        out = []
        now = datetime.now()
        ttl_seconds = 30 * 60
        for row in rows:
            if hasattr(row, 'keys'):
                n_type = row['notif_type'] or 'announcement'
                created_at = row['created_at']
                if n_type == 'announcement':
                    ts = _parse_ts(created_at)
                    if ts and (now - ts).total_seconds() > ttl_seconds:
                        continue
                out.append({
                    "id": row['id'],
                    "title": row['title'] or 'Notification',
                    "message": row['message'] or '',
                    "type": n_type,
                    "source": row['source'] or 'admin',
                    "is_read": bool(row['is_read'] or 0),
                    "created_at": created_at
                })
            else:
                n_type = row[3] or 'announcement'
                created_at = row[6]
                if n_type == 'announcement':
                    ts = _parse_ts(created_at)
                    if ts and (now - ts).total_seconds() > ttl_seconds:
                        continue
                out.append({
                    "id": row[0],
                    "title": row[1] or 'Notification',
                    "message": row[2] or '',
                    "type": n_type,
                    "source": row[4] or 'admin',
                    "is_read": bool(row[5] or 0),
                    "created_at": created_at
                })
        return jsonify(out)
    except Exception as e:
        logger.error(f"Get notifications error: {e}")
        if conn:
            try:
                conn.close()
            except:
                pass
        return jsonify([]), 500

@app.route('/api/notifications/read', methods=['POST'])
def mark_notifications_read():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    conn = None
    try:
        conn, db_type = get_db()
        c = get_cursor(conn, db_type)
        if db_type == 'postgres':
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
        else:
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
        if db_type == 'postgres':
            c.execute("UPDATE user_notifications SET is_read = 1 WHERE user_id = %s", (session['user_id'],))
        else:
            c.execute("UPDATE user_notifications SET is_read = 1 WHERE user_id = ?", (session['user_id'],))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Mark notifications read error: {e}")
        if conn:
            try:
                conn.close()
            except:
                pass
        return jsonify({"error": str(e)}), 500


# ========== USER ACTIVITY & DATA RETENTION ENDPOINTS ==========

@app.route('/api/user_activity')
def get_user_activity():
    """Get user's complete activity history from user_activity_logs"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        # Get query parameters
        limit = min(int(request.args.get('limit', 100)), 500)
        offset = int(request.args.get('offset', 0))
        action_filter = request.args.get('action')
        
        # Ensure table exists
        if db_type == 'postgres':
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_activity_logs (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    google_id TEXT,
                    email TEXT,
                    action TEXT NOT NULL,
                    details TEXT,
                    ip_address TEXT,
                    user_agent TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_activity_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    google_id TEXT,
                    email TEXT,
                    action TEXT NOT NULL,
                    details TEXT,
                    ip_address TEXT,
                    user_agent TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        conn.commit()
        
        # Build query
        where_clause = "WHERE user_id = %s" if db_type == 'postgres' else "WHERE user_id = ?"
        params = [session['user_id']]
        
        if action_filter:
            where_clause += " AND action = %s" if db_type == 'postgres' else " AND action = ?"
            params.append(action_filter)
        
        # Get total count
        count_query = f"SELECT COUNT(*) FROM user_activity_logs {where_clause}"
        c.execute(count_query, tuple(params))
        total = c.fetchone()[0]
        
        # Get activities
        query = f"""
            SELECT id, action, details, ip_address, timestamp
            FROM user_activity_logs
            {where_clause}
            ORDER BY timestamp DESC
            LIMIT %s OFFSET %s
        """ if db_type == 'postgres' else f"""
            SELECT id, action, details, ip_address, timestamp
            FROM user_activity_logs
            {where_clause}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        c.execute(query, tuple(params))
        
        rows = c.fetchall()
        activities = []
        for row in rows:
            if isinstance(row, dict) or hasattr(row, 'keys'):
                activities.append({
                    "id": row['id'],
                    "action": row['action'],
                    "details": json.loads(row['details']) if row['details'] else {},
                    "ip_address": row['ip_address'],
                    "timestamp": row['timestamp']
                })
            else:
                activities.append({
                    "id": row[0],
                    "action": row[1],
                    "details": json.loads(row[2]) if row[2] else {},
                    "ip_address": row[3],
                    "timestamp": row[4]
                })
        
        return jsonify({
            "activities": activities,
            "total": total,
            "limit": limit,
            "offset": offset
        })
    except Exception as e:
        logger.error(f"Get user activity error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/user_signup_info')
def get_user_signup_info():
    """Get user's original signup information for ID retention verification"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        # Ensure table exists
        if db_type == 'postgres':
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_signup_logs (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER UNIQUE NOT NULL,
                    google_id TEXT UNIQUE NOT NULL,
                    email TEXT NOT NULL,
                    name TEXT,
                    first_signup_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_login_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    signup_ip TEXT,
                    total_logins INTEGER DEFAULT 1
                )
            """)
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_signup_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE NOT NULL,
                    google_id TEXT UNIQUE NOT NULL,
                    email TEXT NOT NULL,
                    name TEXT,
                    first_signup_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_login_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    signup_ip TEXT,
                    total_logins INTEGER DEFAULT 1
                )
            """)
        conn.commit()
        
        # Get user info
        if db_type == 'postgres':
            c.execute("SELECT google_id FROM users WHERE id = %s", (session['user_id'],))
        else:
            c.execute("SELECT google_id FROM users WHERE id = ?", (session['user_id'],))
        user_row = c.fetchone()
        google_id = user_row[0] if user_row else None
        
        if not google_id:
            return jsonify({"error": "User not found"}), 404
        
        # Get signup info
        if db_type == 'postgres':
            c.execute("""
                SELECT user_id, google_id, email, name, first_signup_at, last_login_at, signup_ip, total_logins
                FROM user_signup_logs
                WHERE google_id = %s
            """, (google_id,))
        else:
            c.execute("""
                SELECT user_id, google_id, email, name, first_signup_at, last_login_at, signup_ip, total_logins
                FROM user_signup_logs
                WHERE google_id = ?
            """, (google_id,))
        
        row = c.fetchone()
        if not row:
            # User exists but no signup log - create one
            if db_type == 'postgres':
                c.execute("""
                    INSERT INTO user_signup_logs 
                    (user_id, google_id, email, name, first_signup_at, last_login_at, signup_ip, total_logins)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (google_id) DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        last_login_at = EXCLUDED.last_login_at,
                        total_logins = user_signup_logs.total_logins + 1
                """, (session['user_id'], google_id, session.get('user_email', 'unknown'), 
                      session.get('user_name', 'unknown'), datetime.now().isoformat(), 
                      datetime.now().isoformat(), 'unknown', 1))
            else:
                c.execute("""
                    INSERT INTO user_signup_logs 
                    (user_id, google_id, email, name, first_signup_at, last_login_at, signup_ip, total_logins)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(google_id) DO UPDATE SET
                        user_id = excluded.user_id,
                        last_login_at = excluded.last_login_at,
                        total_logins = user_signup_logs.total_logins + 1
                """, (session['user_id'], google_id, session.get('user_email', 'unknown'), 
                      session.get('user_name', 'unknown'), datetime.now().isoformat(), 
                      datetime.now().isoformat(), 'unknown', 1))
            conn.commit()
            
            return jsonify({
                "user_id": session['user_id'],
                "google_id": google_id,
                "email": session.get('user_email'),
                "name": session.get('user_name'),
                "first_signup_at": datetime.now().isoformat(),
                "last_login_at": datetime.now().isoformat(),
                "signup_ip": 'unknown',
                "total_logins": 1,
                "id_retained": True
            })
        
        if isinstance(row, dict) or hasattr(row, 'keys'):
            return jsonify({
                "user_id": row['user_id'],
                "google_id": row['google_id'],
                "email": row['email'],
                "name": row['name'],
                "first_signup_at": row['first_signup_at'],
                "last_login_at": row['last_login_at'],
                "signup_ip": row['signup_ip'],
                "total_logins": row['total_logins'],
                "id_retained": row['user_id'] == session['user_id']
            })
        else:
            return jsonify({
                "user_id": row[0],
                "google_id": row[1],
                "email": row[2],
                "name": row[3],
                "first_signup_at": row[4],
                "last_login_at": row[5],
                "signup_ip": row[6],
                "total_logins": row[7],
                "id_retained": row[0] == session['user_id']
            })
    except Exception as e:
        logger.error(f"Get signup info error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/user_data_summary')
def get_user_data_summary():
    """Get summary of all user data for data retention verification"""
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    conn, db_type = get_db()
    c = get_cursor(conn, db_type)
    
    try:
        user_id = session['user_id']
        summary = {"user_id": user_id, "data_retained": {}}
        
        tables_to_check = [
            ('likes', 'user_id'),
            ('saves', 'user_id'),
            ('comments', 'user_id'),
            ('community_messages', 'user_id'),
            ('comment_replies', 'user_id'),
            ('daily_actions', 'user_id'),
            ('user_activity_logs', 'user_id'),
            ('collections', 'user_id')
        ]
        
        for table, column in tables_to_check:
            try:
                if db_type == 'postgres':
                    c.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = %s", (user_id,))
                else:
                    c.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (user_id,))
                count = c.fetchone()[0]
                summary["data_retained"][table] = count
            except Exception:
                summary["data_retained"][table] = 0
        
        # Get earliest activity
        earliest_dates = []
        for table, column in tables_to_check:
            try:
                if db_type == 'postgres':
                    c.execute(f"SELECT MIN(timestamp) FROM {table} WHERE {column} = %s", (user_id,))
                else:
                    c.execute(f"SELECT MIN(timestamp) FROM {table} WHERE {column} = ?", (user_id,))
                result = c.fetchone()
                if result and result[0]:
                    earliest_dates.append(result[0])
            except Exception:
                pass
        
        summary["total_records"] = sum(summary["data_retained"].values())
        summary["data_retention_active"] = summary["total_records"] > 0
        
        return jsonify(summary)
    except Exception as e:
        logger.error(f"Get data summary error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

