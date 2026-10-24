import csv, io, logging, os, sqlite3, re
from datetime import datetime, timezone
from pathlib import Path
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update, ForceReply
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aeh")
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_ORDER = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
INITIAL_ADMINS = set(ADMIN_ID_ORDER)
OWNER_RAW = os.getenv("OWNER_ID", "").strip()
OWNER = int(OWNER_RAW) if OWNER_RAW.isdigit() else (ADMIN_ID_ORDER[0] if ADMIN_ID_ORDER else None)
DB_PATH = os.getenv("DB_PATH", "aeh.sqlite3")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_USE_POSTGRES = bool(DATABASE_URL)
WELCOME_PHOTO_PATH = Path(__file__).resolve().parent / "assets" / "welcome.jpg"
LABELS = {"pending": "قيد المراجعة", "approved": "مقبول", "rejected": "مرفوض"}

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def csv_safe(value):
    # Prevent spreadsheet formula execution when exported fields are opened in Excel/Sheets.
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value

class _HybridRow(dict):
    def __init__(self, names, values):
        super().__init__(zip(names, values)); self._values = tuple(values)
    def __getitem__(self, key):
        return self._values[key] if isinstance(key, int) else super().__getitem__(key)

class _PgCursor:
    def __init__(self, cursor, names=None, lastrowid=None): self._cursor, self._names, self.lastrowid = cursor, names or [], lastrowid
    def _row(self, row):
        if row is None: return None
        if isinstance(row, dict):
            names = self._names or list(row.keys())
            return _HybridRow(names, [row[k] for k in names])
        return row
    def fetchone(self): return self._row(self._cursor.fetchone())
    def fetchall(self): return [self._row(r) for r in self._cursor.fetchall()]
    def __iter__(self):
        for row in self._cursor: yield self._row(row)
    @property
    def rowcount(self): return self._cursor.rowcount

class _PgConnection:
    def __init__(self, raw): self.raw = raw
    def __enter__(self): self.raw.__enter__(); return self
    def __exit__(self, typ, val, tb): return self.raw.__exit__(typ, val, tb)
    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip(): self.execute(statement)
    def execute(self, sql, params=()):
        sql = sql.strip()
        match = re.match(r"PRAGMA\s+table_info\(([^)]+)\)", sql, re.I)
        if match:
            cur = self.raw.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s ORDER BY ordinal_position", (match.group(1).strip(),))
            return _PgCursor(cur, ["name"])
        ignored = bool(re.search(r"INSERT\s+OR\s+IGNORE\s+INTO", sql, re.I))
        replacing = bool(re.search(r"INSERT\s+OR\s+REPLACE\s+INTO", sql, re.I))
        sql = re.sub(r"INSERT\s+OR\s+(?:IGNORE|REPLACE)\s+INTO", "INSERT INTO", sql, flags=re.I).replace("?", "%s")
        if ignored: sql += " ON CONFLICT DO NOTHING"
        if replacing and "payout_prompts" in sql.lower(): sql += " ON CONFLICT (admin_id,prompt_message_id) DO UPDATE SET request_id=EXCLUDED.request_id,created_at=EXCLUDED.created_at"
        if replacing and "payout_drafts" in sql.lower(): sql += " ON CONFLICT (request_id,admin_id) DO UPDATE SET photo=EXCLUDED.photo,created_at=EXCLUDED.created_at"
        wants_id = bool(re.search(r"INSERT\s+INTO\s+requests\s*\(", sql, re.I))
        if wants_id and "RETURNING" not in sql.upper(): sql += " RETURNING id"
        cur = self.raw.execute(sql, params)
        names = [d.name for d in cur.description] if cur.description else []
        lastrowid = None
        if wants_id and cur.description:
            row = cur.fetchone()
            if row: lastrowid = row.get("id") if isinstance(row, dict) else row[0]
            cur = self.raw.execute("SELECT %s::bigint AS id", (lastrowid,)); names = ["id"]
        return _PgCursor(cur, names, lastrowid)

def db():
    if _USE_POSTGRES:
        import psycopg
        from psycopg.rows import dict_row
        return _PgConnection(psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10))
    p = Path(DB_PATH)
    if str(p.parent) not in ("", "."): p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=20); c.row_factory = sqlite3.Row; return c

def _init_postgres_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS programs(id INTEGER PRIMARY KEY CHECK(id BETWEEN 1 AND 10),name TEXT NOT NULL,code TEXT NOT NULL DEFAULT '',enabled INTEGER NOT NULL DEFAULT 1,exchange_rate TEXT NOT NULL DEFAULT '50');
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS requests(id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,username TEXT,full_name TEXT,program_id INTEGER,program_name TEXT,program_code TEXT,wallet TEXT,customer_id TEXT,amount TEXT,photo TEXT,payout_photo TEXT,payout_sent_at TEXT,status TEXT DEFAULT 'pending',created_at TEXT,reviewed_at TEXT,reviewer_id BIGINT,reason TEXT,exchange_rate TEXT);
        CREATE TABLE IF NOT EXISTS payout_prompts(admin_id BIGINT NOT NULL,prompt_message_id BIGINT NOT NULL,request_id BIGINT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(admin_id,prompt_message_id));
        CREATE TABLE IF NOT EXISTS payout_drafts(request_id BIGINT NOT NULL,admin_id BIGINT NOT NULL,photo TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(request_id,admin_id));
        CREATE INDEX IF NOT EXISTS request_status_idx ON requests(status);
        CREATE TABLE IF NOT EXISTS admins(user_id BIGINT PRIMARY KEY,added_by BIGINT,added_at TEXT);
        CREATE TABLE IF NOT EXISTS user_blocks(user_id BIGINT PRIMARY KEY,blocked_by BIGINT NOT NULL,blocked_at TEXT NOT NULL,reason TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS bot_users(user_id BIGINT PRIMARY KEY,username TEXT,full_name TEXT,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,welcome_photo_sent INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS activity(id BIGSERIAL PRIMARY KEY,actor BIGINT,action TEXT,details TEXT,created_at TEXT);
        """)
        for table,column,definition in [("requests","customer_id","TEXT"),("requests","payout_photo","TEXT"),("requests","payout_sent_at","TEXT"),("requests","exchange_rate","TEXT"),("programs","exchange_rate","TEXT NOT NULL DEFAULT '50'"),("bot_users","welcome_photo_sent","INTEGER NOT NULL DEFAULT 0")]:
            exists=c.execute("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=? AND column_name=?",(table,column)).fetchone()
            if not exists: c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        for i in range(1,11): c.execute("INSERT INTO programs(id,name,code,enabled,exchange_rate) VALUES(?,?,?,1,'50') ON CONFLICT DO NOTHING",(i,f"برنامج {i:02d}","لم يحدد الكود بعد"))
        c.execute("INSERT INTO settings(key,value) VALUES('exchange_rate','50') ON CONFLICT DO NOTHING")
        if c.execute("SELECT COUNT(*) FROM admins").fetchone()[0] == 0:
            for aid in INITIAL_ADMINS: c.execute("INSERT INTO admins VALUES(?,?,?) ON CONFLICT DO NOTHING",(aid,aid,now()))
        if OWNER is not None: c.execute("INSERT INTO admins VALUES(?,?,?) ON CONFLICT DO NOTHING",(OWNER,OWNER,now()))

def _init_sqlite_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS programs(id INTEGER PRIMARY KEY CHECK(id BETWEEN 1 AND 10),name TEXT NOT NULL,code TEXT NOT NULL DEFAULT '',enabled INTEGER NOT NULL DEFAULT 1,exchange_rate TEXT NOT NULL DEFAULT '50');
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,username TEXT,full_name TEXT,program_id INTEGER,program_name TEXT,program_code TEXT,wallet TEXT,customer_id TEXT,amount TEXT,photo TEXT,payout_photo TEXT,payout_sent_at TEXT,status TEXT DEFAULT 'pending',created_at TEXT,reviewed_at TEXT,reviewer_id INTEGER,reason TEXT,exchange_rate TEXT);
        CREATE TABLE IF NOT EXISTS payout_prompts(admin_id INTEGER NOT NULL,prompt_message_id INTEGER NOT NULL,request_id INTEGER NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(admin_id,prompt_message_id));
        CREATE TABLE IF NOT EXISTS payout_drafts(request_id INTEGER NOT NULL,admin_id INTEGER NOT NULL,photo TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(request_id,admin_id));
        CREATE INDEX IF NOT EXISTS request_status_idx ON requests(status);
        CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY,added_by INTEGER,added_at TEXT);
        CREATE TABLE IF NOT EXISTS user_blocks(user_id INTEGER PRIMARY KEY,blocked_by INTEGER NOT NULL,blocked_at TEXT NOT NULL,reason TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS bot_users(user_id INTEGER PRIMARY KEY,username TEXT,full_name TEXT,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,welcome_photo_sent INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY AUTOINCREMENT,actor INTEGER,action TEXT,details TEXT,created_at TEXT);
        """)
        # Migrate existing databases without losing previously saved requests.
        request_columns = {row["name"] for row in c.execute("PRAGMA table_info(requests)")}
        if "customer_id" not in request_columns:
            c.execute("ALTER TABLE requests ADD COLUMN customer_id TEXT")
        if "payout_photo" not in request_columns:
            c.execute("ALTER TABLE requests ADD COLUMN payout_photo TEXT")
        if "payout_sent_at" not in request_columns:
            c.execute("ALTER TABLE requests ADD COLUMN payout_sent_at TEXT")
        if "exchange_rate" not in request_columns:
            c.execute("ALTER TABLE requests ADD COLUMN exchange_rate TEXT")
        program_columns = {row["name"] for row in c.execute("PRAGMA table_info(programs)")}
        if "exchange_rate" not in program_columns:
            c.execute("ALTER TABLE programs ADD COLUMN exchange_rate TEXT NOT NULL DEFAULT '50'")
        user_columns = {row["name"] for row in c.execute("PRAGMA table_info(bot_users)")}
        if "welcome_photo_sent" not in user_columns:
            c.execute("ALTER TABLE bot_users ADD COLUMN welcome_photo_sent INTEGER NOT NULL DEFAULT 0")
        for i in range(1,11):
            c.execute("INSERT OR IGNORE INTO programs(id,name,code,enabled,exchange_rate) VALUES(?,?,?,1,'50')",(i,f"برنامج {i:02d}","لم يحدد الكود بعد"))
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('exchange_rate','50')")
        # Seed configured admins only on first database initialization. Do not
        # re-add deleted admins on every restart.
        admin_count = c.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
        if admin_count == 0:
            for aid in INITIAL_ADMINS:
                c.execute("INSERT OR IGNORE INTO admins VALUES(?,?,?)",(aid,aid,now()))
        if OWNER is not None:
            c.execute("INSERT OR IGNORE INTO admins VALUES(?,?,?)",(OWNER,OWNER,now()))

def init_db():
    if _USE_POSTGRES: return _init_postgres_db()
    return _init_sqlite_db()

e_db()

def admins():
    with db() as c: return [r[0] for r in c.execute("SELECT user_id FROM admins")]
def is_admin(uid): return uid in admins()
def owner(uid): return uid == OWNER
def is_blocked(uid):
    with db() as c: return c.execute("SELECT 1 FROM user_blocks WHERE user_id=?",(uid,)).fetchone() is not None
def audit(uid, action, details=""):
    with db() as c: c.execute("INSERT INTO activity(actor,action,details,created_at) VALUES(?,?,?,?)",(uid,action,details[:500],now()))
def get_programs(active=False):
    with db() as c: return c.execute("SELECT * FROM programs"+(" WHERE enabled=1" if active else "")+" ORDER BY id").fetchall()
def get_exchange_rate():
    with db() as c:
        row=c.execute("SELECT value FROM settings WHERE key='exchange_rate'").fetchone()
    try:
        rate=float(row["value"]) if row else 50.0
        return rate if rate > 0 else 50.0
    except (TypeError,ValueError):
        return 50.0
def get_program_rate(program_id):
    with db() as c:
        row=c.execute("SELECT exchange_rate FROM programs WHERE id=?",(program_id,)).fetchone()
    try:
        rate=float(row["exchange_rate"]) if row and row["exchange_rate"] else 50.0
        return rate if rate > 0 else 50.0
    except (TypeError,ValueError):
        return 50.0

def money_text(value):
    return f"{value:,.2f}".rstrip("0").rstrip(".")
def home_kb(uid=None):
    ps=get_programs(True); rows=[]
    for i in range(0,len(ps),2): rows.append([InlineKeyboardButton(p["name"],callback_data=f"p:{p['id']}") for p in ps[i:i+2]])
    if not rows: rows=[[InlineKeyboardButton("لا توجد برامج متاحة",callback_data="noop")]]
    # Full-width URL button opens the client charging platform directly.
    rows.append([InlineKeyboardButton("منصة الإمبراطور للشحن",url="https://Embrator.store")])
    if uid is not None and is_admin(uid): rows.append([InlineKeyboardButton("لوحة الإدارة",callback_data="a:home")])
    return InlineKeyboardMarkup(rows)
def admin_kb():
    return InlineKeyboardMarkup([
    [InlineKeyboardButton("الطلبات المعلقة",callback_data="a:pending"),InlineKeyboardButton("كل الطلبات",callback_data="a:all")],
    [InlineKeyboardButton("إدارة البرامج",callback_data="a:programs")],
    [InlineKeyboardButton("الإحصائيات",callback_data="a:stats")],
    [InlineKeyboardButton("المستخدمون",callback_data="a:users"),InlineKeyboardButton("المشرفون",callback_data="a:admins")],
    [InlineKeyboardButton("تصدير CSV",callback_data="a:export"),InlineKeyboardButton("سجل النشاط",callback_data="a:log")]])
def back_kb(target="a:home"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("رجوع للقائمة الرئيسية",callback_data=target)]])
async def edit(q,text,kb=None):
    try: await q.edit_message_text(text,reply_markup=kb)
    except Exception: await q.message.reply_text(text,reply_markup=kb)

async def start(u,ctx):
    if not u.effective_chat or u.effective_chat.type != "private":
        return await u.effective_message.reply_text("لحماية بياناتك، استخدم البوت في المحادثة الخاصة فقط.")
    if is_blocked(u.effective_user.id) and not is_admin(u.effective_user.id):
        return await u.message.reply_text("لا يمكنك استخدام البوت حاليًا. تواصل مع الإدارة.")
    with db() as c:
        c.execute("INSERT INTO bot_users(user_id,username,full_name,first_seen,last_seen) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,full_name=excluded.full_name,last_seen=excluded.last_seen",(u.effective_user.id,u.effective_user.username,u.effective_user.full_name,now(),now()))
    ctx.user_data.clear()
    greeting="أهلًا بك، اختر البرنامج المطلوب."
    keyboard=home_kb(u.effective_user.id)
    # Send the welcome image on every /start so it remains visible in the chat.
    try:
        with WELCOME_PHOTO_PATH.open("rb") as photo:
            await u.message.reply_photo(photo=photo,caption=greeting,reply_markup=keyboard)
    except Exception:
        log.exception("Welcome photo could not be sent to user %s",u.effective_user.id)
        return await u.message.reply_text(greeting,reply_markup=keyboard)
async def admin_cmd(u,ctx):
    if not u.effective_chat or u.effective_chat.type != "private":
        return await u.effective_message.reply_text("لوحة الإدارة متاحة في الخاص فقط.")
    if not is_admin(u.effective_user.id): return await u.message.reply_text("هذا القسم للإدارة فقط.")
    await u.message.reply_text("لوحة تحكم الإدارة",reply_markup=admin_kb())
async def cancel(u,ctx):
    if not u.effective_chat or u.effective_chat.type != "private":
        return await u.effective_message.reply_text("استخدم البوت في الخاص فقط لحماية البيانات.")
    ctx.user_data.clear(); await u.message.reply_text("أهلًا بك، اختر البرنامج المطلوب.",reply_markup=home_kb(u.effective_user.id))
async def setprogram(u,ctx):
    if not u.effective_chat or u.effective_chat.type != "private":
        return await u.effective_message.reply_text("استخدم البوت في الخاص فقط لحماية البيانات.")
    uid=u.effective_user.id
    if not is_admin(uid): return await u.message.reply_text("غير مصرح.")
    parts=[x.strip() for x in u.message.text.partition(" ")[2].split("|",2)]
    if len(parts)!=3 or not parts[0].isdigit() or not 1<=int(parts[0])<=10 or not parts[1] or not parts[2]:
        return await u.message.reply_text("الاستخدام: /setprogram رقم | الاسم | الكود")
    with db() as c: c.execute("UPDATE programs SET name=?,code=? WHERE id=?",(parts[1][:80],parts[2][:200],int(parts[0])))
    audit(uid,"program_updated",parts[0]+" "+parts[1]); await u.message.reply_text("تم تحديث البرنامج.")
async def addadmin(u,ctx):
    if not u.effective_chat or u.effective_chat.type != "private":
        return await u.effective_message.reply_text("استخدم البوت في الخاص فقط لحماية البيانات.")
    uid=u.effective_user.id
    if not owner(uid): return await u.message.reply_text("مالك البوت فقط يمكنه إدارة المشرفين.")
    if not ctx.args or not ctx.args[0].isdigit(): return await u.message.reply_text("الاستخدام: /addadmin TELEGRAM_ID")
    target=int(ctx.args[0])
    with db() as c: c.execute("INSERT OR IGNORE INTO admins VALUES(?,?,?)",(target,uid,now()))
    audit(uid,"admin_added",str(target)); await u.message.reply_text("تمت إضافة المشرف.")
async def deladmin(u,ctx):
    if not u.effective_chat or u.effective_chat.type != "private":
        return await u.effective_message.reply_text("استخدم البوت في الخاص فقط لحماية البيانات.")
    uid=u.effective_user.id
    if not owner(uid): return await u.message.reply_text("مالك البوت فقط يمكنه إدارة المشرفين.")
    if not ctx.args or not ctx.args[0].isdigit(): return await u.message.reply_text("الاستخدام: /deladmin TELEGRAM_ID")
    target=int(ctx.args[0])
    if target==OWNER: return await u.message.reply_text("لا يمكن حذف المالك الأساسي.")
    if target not in admins(): return await u.message.reply_text("هذا الرقم ليس مشرفًا مسجلًا.")
    with db() as c: c.execute("DELETE FROM admins WHERE user_id=?",(target,))
    audit(uid,"admin_removed",str(target)); await u.message.reply_text("تم حذف المشرف إن كان موجودًا.")
async def usercmd(u,ctx):
    if not u.effective_chat or u.effective_chat.type != "private":
        return await u.effective_message.reply_text("استخدم البوت في الخاص فقط لحماية البيانات.")
    if not is_admin(u.effective_user.id): return await u.message.reply_text("غير مصرح.")
    if not ctx.args or not ctx.args[0].isdigit(): return await u.message.reply_text("الاستخدام: /user TELEGRAM_ID")
    with db() as c: rs=c.execute("SELECT id,program_name,amount,status FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 20",(int(ctx.args[0]),)).fetchall()
    await u.message.reply_text("\n".join([f"طلبات المستخدم {ctx.args[0]}:"]+[f"#{r['id']} {r['program_name']} | {r['amount']} | {LABELS[r['status']]}" for r in rs]) if rs else "لا توجد طلبات.")
async def list_requests(q,status,page=0):
    with db() as c:
        where="" if status=="all" else " WHERE status='pending'"
        rs=c.execute("SELECT * FROM requests"+where+" ORDER BY id DESC LIMIT 5 OFFSET ?",(page*5,)).fetchall()
        total=c.execute("SELECT COUNT(*) FROM requests"+where).fetchone()[0]
    kb=[[InlineKeyboardButton(f"#{r['id']} {r['program_name']} · {LABELS[r['status']]}",callback_data=f"r:{r['id']}")] for r in rs]
    nav=[]
    if page: nav.append(InlineKeyboardButton("السابق",callback_data=f"page:{status}:{page-1}"))
    if (page+1)*5<total: nav.append(InlineKeyboardButton("التالي",callback_data=f"page:{status}:{page+1}"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton("رجوع",callback_data="a:home")])
    await edit(q,("الطلبات المعلقة" if status=="pending" else "كل الطلبات")+("\nلا توجد طلبات." if not rs else "\nاختر طلبًا لعرض التفاصيل."),InlineKeyboardMarkup(kb))
async def request_detail(q,rid):
    with db() as c: r=c.execute("SELECT * FROM requests WHERE id=?",(rid,)).fetchone()
    if not r: return await edit(q,"الطلب غير موجود.",admin_kb())
    text=f"طلب #{rid}\nالحالة: {LABELS[r['status']]}\nالمستخدم: {r['full_name']} (@{r['username'] or 'بدون اسم'})\nTelegram ID: {r['user_id']}\nالبرنامج: {r['program_name']}\nالكود: {r['program_code']}\nرقم الكاش: {r['wallet']}\nID العميل: {r['customer_id'] or '—'}\nعدد الدولارات: {r['amount']}\nالتاريخ: {r['created_at']}\nسبب الرفض: {r['reason'] or '—'}\n\nتحقق من وصول التحويل بشكل مستقل."
    buttons=[]
    if r["status"]=="pending": buttons.append([InlineKeyboardButton("قبول",callback_data=f"ok:{rid}"),InlineKeyboardButton("رفض",callback_data=f"no:{rid}")])
    else: buttons.append([InlineKeyboardButton("إعادة للمراجعة",callback_data=f"reopen:{rid}")])
    buttons.append([InlineKeyboardButton("رجوع",callback_data="a:pending")])
    await edit(q,text,InlineKeyboardMarkup(buttons))
    try: await q.message.reply_photo(r["photo"],caption=f"اسكرين شوت التارجت للطلب #{rid}")
    except Exception as e: log.warning("Photo display failed: %s",e)
async def decide(q,ctx,rid,status,reason=""):
    uid=q.from_user.id
    with db() as c:
        r=c.execute("SELECT * FROM requests WHERE id=?",(rid,)).fetchone()
        if not r: return await edit(q,"الطلب غير موجود.")
        if r["status"]!="pending": return await edit(q,"تم اتخاذ قرار سابق على هذا الطلب.",admin_kb())
        c.execute("UPDATE requests SET status=?,reviewed_at=?,reviewer_id=?,reason=? WHERE id=? AND status='pending'",(status,now(),uid,reason,rid))
    audit(uid,"request_"+status,f"#{rid} {reason}")
    if status=="approved":
        rate=float(r["exchange_rate"]) if r["exchange_rate"] else get_program_rate(r["program_id"])
        usd=float(r["amount"] or 0)
        egp=usd*rate
        egp_text=money_text(egp)
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("رفع صورة إثبات التحويل",callback_data=f"payout_upload:{rid}")],[InlineKeyboardButton("عرض الطلب",callback_data=f"r:{rid}")]])
        await edit(q,f"تم تأكيد وصول الدولارات للوكالة للطلب #{rid}.\n\nالمبلغ المستحق: {egp_text} جنيه مصري (سعر الصرف {money_text(rate)} جنيه للدولار).\nحوّل المبلغ للمستخدم من خارج البوت، ثم اضغط «رفع صورة إثبات التحويل». ستظهر لك معاينة للصورة قبل إرسالها للمستخدم.",kb)
        try: await ctx.bot.send_message(r["user_id"],f"تم تأكيد وصول الدولارات الخاصة بطلبك رقم #{rid}. طلبك الآن في مرحلة تحويل المبلغ بالجنيه المصري، وسيصلك إثبات التحويل بعد إتمام الدفع.")
        except Exception: pass
        return
    msg=f"تم رفض طلبك رقم #{rid}. السبب: {reason}\n\nللاستفسار، تواصل مع خدمة العملاء: 01286591021"
    try: await ctx.bot.send_message(r["user_id"],msg)
    except Exception: pass
    await edit(q,f"تم رفض الطلب #{rid} وإبلاغ المستخدم.",admin_kb())
async def callbacks(u,ctx):
    q=u.callback_query; await q.answer(); uid=q.from_user.id; d=q.data
    if not q.message or q.message.chat.type != "private":
        return await q.answer("استخدم البوت في الخاص فقط لحماية البيانات.", show_alert=True)
    if d=="noop": return
    if d.startswith("p:"):
        if is_blocked(uid) and not is_admin(uid):
            return await edit(q,"لا يمكنك استخدام البوت حاليًا. تواصل مع الإدارة.")
        pid=int(d.split(":")[1])
        with db() as c: p=c.execute("SELECT * FROM programs WHERE id=? AND enabled=1",(pid,)).fetchone()
        if not p: return await edit(q,"البرنامج غير متاح.",home_kb())
        ctx.user_data.clear(); ctx.user_data["draft"]={"pid":pid,"name":p["name"],"code":p["code"],"exchange_rate":float(p["exchange_rate"] or 50)}; ctx.user_data["step"]="confirm_program"
        rate=float(ctx.user_data["draft"].get("exchange_rate",50))
        welcome = (f"هلا بيك {q.from_user.first_name or ''}، معك الإمبراطور للشحن والسحب.\n\n" + "ارسل التارجت الخاص بك للوكالة.\n\n" + f"التارجت الخاص بك: {p['code'] or 'لم يحدد الكود بعد'}\n\n" + f"سعر الصرف الخاص بهذا البرنامج: الدولار = {money_text(rate)} جنيه مصري.")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("متابعة",callback_data="continue_request"),InlineKeyboardButton("لا",callback_data="ucancel")]])
        return await edit(q,welcome,kb)
    if d=="continue_request":
        if ctx.user_data.get("step")!="confirm_program" or not ctx.user_data.get("draft"):
            return await edit(q,"اختار البرنامج من القائمة أولًا.",home_kb(uid))
        ctx.user_data["step"]="proof"
        checklist = ("• البيانات المطلوبة ⚡\n" + "• اسكرين شوت للتارجت الذي أرسلته.\n" + "• رقم الكاش الخاص بك.\n" + "• الـ ID الخاص بك.\n" + "• عدد الدولارات التي أرسلتها.")
        return await edit(q,checklist + "\n\nالخطوة 1 من 4 — أرسل اسكرين شوت التارجت كصورة داخل تيليجرام.",InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء العملية",callback_data="ucancel")]]))
    if d=="ucancel": ctx.user_data.clear(); return await edit(q,"أهلًا بك، اختر البرنامج المطلوب.",home_kb(uid))
    if not is_admin(uid): return await q.answer("غير مصرح.",show_alert=True)
    if d.startswith("payout_upload:"):
        rid=int(d.split(":")[1])
        with db() as c: r=c.execute("SELECT * FROM requests WHERE id=?",(rid,)).fetchone()
        if not r or r["status"]!="approved" or r["payout_sent_at"]:
            return await edit(q,"هذا الطلب غير موجود أو لم يعد بحاجة إلى إثبات دفع.",admin_kb())
        prompt=await q.message.reply_text(f"الطلب #{rid} — بعد إجراء التحويل خارج البوت، ردّ على هذه الرسالة بصورة التحويل. ستظهر لك معاينة وزر إرسال قبل وصولها للمستخدم.",reply_markup=ForceReply(selective=True))
        with db() as c: c.execute("INSERT OR REPLACE INTO payout_prompts(admin_id,prompt_message_id,request_id,created_at) VALUES(?,?,?,?)",(uid,prompt.message_id,rid,now()))
        return
    if d.startswith("payout_cancel:"):
        rid=int(d.split(":")[1])
        with db() as c: c.execute("DELETE FROM payout_drafts WHERE request_id=? AND admin_id=?",(rid,uid))
        return await edit(q,f"تم إلغاء إرسال الإثبات للطلب #{rid}. لم يتم إرسال شيء للمستخدم.",InlineKeyboardMarkup([[InlineKeyboardButton("إعادة رفع صورة الإثبات",callback_data=f"payout_upload:{rid}")],[InlineKeyboardButton("لوحة الإدارة",callback_data="a:home")]]))
    if d.startswith("payout_send:"):
        rid=int(d.split(":")[1])
        with db() as c:
            r=c.execute("SELECT * FROM requests WHERE id=?",(rid,)).fetchone()
            draft=c.execute("SELECT photo FROM payout_drafts WHERE request_id=? AND admin_id=?",(rid,uid)).fetchone()
            if not r or r["status"]!="approved" or not draft or r["payout_sent_at"]:
                return await edit(q,"لا يمكن إرسال الإثبات: الطلب غير معتمد، أو لا توجد معاينة صالحة، أو تم إرسال الإثبات مسبقًا.")
            cur=c.execute("UPDATE requests SET payout_sent_at='SENDING' WHERE id=? AND status='approved' AND payout_sent_at IS NULL",(rid,))
            if cur.rowcount!=1: return await edit(q,"تم بدء إرسال إثبات هذا الطلب بالفعل أو تم إرساله.")
            photo_id=draft["photo"]
        usd=float(r["amount"] or 0); rate=float(r["exchange_rate"]) if r["exchange_rate"] else get_program_rate(r["program_id"]); egp=usd*rate; egp_text=money_text(egp)
        caption=(f"✅ تم دفع المبلغ المستحق وقدره: {egp_text} جنيه مصري.\n\n"
                 "📎 مرفق لحضرتك إثبات التحويل.\n\n"
                 "كان معكم الإمبراطور للسحب والشحن.\n"
                 "لأي مشكلة أو استفسار، تواصل مع خدمة العملاء: 01286591021.")
        try: await ctx.bot.send_photo(r["user_id"],photo_id,caption=caption)
        except Exception as e:
            log.exception("Could not send payout proof to user for request %s: %s",rid,e)
            with db() as c: c.execute("UPDATE requests SET payout_sent_at=NULL WHERE id=? AND payout_sent_at='SENDING'",(rid,))
            return await edit(q,"تعذر إرسال الإثبات للمستخدم. لم يتم تأكيد الدفع. يمكنك المحاولة مرة أخرى من المعاينة.")
        with db() as c:
            c.execute("UPDATE requests SET payout_photo=?,payout_sent_at=? WHERE id=? AND payout_sent_at='SENDING'",(photo_id,now(),rid))
            c.execute("DELETE FROM payout_drafts WHERE request_id=? AND admin_id=?",(rid,uid))
            c.execute("DELETE FROM payout_prompts WHERE request_id=?",(rid,))
        audit(uid,"payout_proof_sent",f"#{rid}")
        return await edit(q,f"تم إرسال صورة إثبات التحويل ورسالة تأكيد الدفع للمستخدم بنجاح للطلب #{rid}.",admin_kb())
    if d=="a:home": return await edit(q,"لوحة تحكم الإدارة",admin_kb())
    if d=="a:pending": return await list_requests(q,"pending")
    if d=="a:all": return await list_requests(q,"all")
    if d.startswith("page:"):
        _,s,p=d.split(":"); return await list_requests(q,s,int(p))
    if d.startswith("r:"): return await request_detail(q,int(d.split(":")[1]))
    if d.startswith("ok:"): return await decide(q,ctx,int(d.split(":")[1]),"approved")
    if d.startswith("no:"):
        ctx.user_data["step"]="reject"; ctx.user_data["rid"]=int(d.split(":")[1])
        return await edit(q,"أرسل سبب الرفض في رسالة واحدة.",InlineKeyboardMarkup([[InlineKeyboardButton("رجوع للطلب",callback_data=f"r:{ctx.user_data['rid']}")],[InlineKeyboardButton("رجوع للقائمة الرئيسية",callback_data="a:home")]]))
    if d.startswith("reopen:"):
        rid=int(d.split(":")[1])
        with db() as c: c.execute("UPDATE requests SET status='pending',reviewed_at=NULL,reviewer_id=NULL,reason=NULL WHERE id=?",(rid,)); r=c.execute("SELECT user_id FROM requests WHERE id=?",(rid,)).fetchone()
        audit(uid,"request_reopened",str(rid))
        if r:
            try: await ctx.bot.send_message(r["user_id"],f"أعيد طلبك #{rid} للمراجعة.")
            except Exception: pass
        return await request_detail(q,rid)
    if d in ("program_edit_confirm","program_edit_cancel"):
        if not is_admin(uid): return await q.answer("غير مصرح.",show_alert=True)
        step=ctx.user_data.get("step")
        pid=ctx.user_data.get("pid")
        value=ctx.user_data.get("pending_program_value")
        if d=="program_edit_cancel":
            ctx.user_data.pop("pending_program_value",None)
            ctx.user_data.pop("step",None)
            ctx.user_data.pop("pid",None)
            return await edit(q,"تم إلغاء التعديل. لم يتم تغيير أي بيانات.",InlineKeyboardMarkup([
                [InlineKeyboardButton("إعدادات البرنامج",callback_data=f"pe:{pid}")] if pid else [InlineKeyboardButton("إدارة البرامج",callback_data="a:programs")],
                [InlineKeyboardButton("القائمة الرئيسية",callback_data="a:home")]
            ]))
        if step not in ("confirm_program_edit_name","confirm_program_edit_code") or not pid or value is None:
            ctx.user_data.pop("pending_program_value",None)
            ctx.user_data.pop("step",None)
            ctx.user_data.pop("pid",None)
            return await edit(q,"انتهت جلسة التعديل. افتح إدارة البرامج وحاول مرة أخرى.",admin_kb())
        if step=="confirm_program_edit_name":
            with db() as c:
                c.execute("UPDATE programs SET name=? WHERE id=?",(value,pid))
                p=c.execute("SELECT name,code FROM programs WHERE id=?",(pid,)).fetchone()
            action="program_name_updated"
            confirmation=f"تم تأكيد تغيير اسم البرنامج رقم {pid}.\\n\\nالاسم الجديد: {p['name']}\\nID البرنامج: {p['code']}"
        else:
            with db() as c:
                c.execute("UPDATE programs SET code=? WHERE id=?",(value,pid))
                p=c.execute("SELECT name,code FROM programs WHERE id=?",(pid,)).fetchone()
            action="program_id_updated"
            confirmation=f"تم تأكيد تغيير ID البرنامج رقم {pid}.\\n\\nاسم البرنامج: {p['name']}\\nالـ ID الجديد: {p['code']}"
        ctx.user_data.clear()
        audit(uid,action,f"{pid}: {value}")
        return await edit(q,confirmation,InlineKeyboardMarkup([
            [InlineKeyboardButton("إدارة البرامج",callback_data="a:programs")],
            [InlineKeyboardButton("القائمة الرئيسية",callback_data="a:home")]
        ]))
    if d=="a:programs":
        return await callbacks_proxy_programs(q)
    if d.startswith("pt:"):
        pid=int(d.split(":")[1])
        with db() as c: c.execute("UPDATE programs SET enabled=1-enabled WHERE id=?",(pid,))
        audit(uid,"program_toggle",str(pid)); return await callbacks_proxy_programs(q)
    if d.startswith("pe:") and d.count(":")==1:
        pid=int(d.split(":")[1])
        with db() as c: p=c.execute("SELECT id,name,code,exchange_rate FROM programs WHERE id=?",(pid,)).fetchone()
        if not p: return await edit(q,"البرنامج غير موجود.",back_kb("a:programs"))
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("تغيير اسم البرنامج",callback_data=f"pe:name:{pid}")],
            [InlineKeyboardButton("تغيير ID البرنامج",callback_data=f"pe:code:{pid}")],
            [InlineKeyboardButton("تغيير سعر الصرف",callback_data=f"pe:rate:{pid}")],
            [InlineKeyboardButton("رجوع لإدارة البرامج",callback_data="a:programs")],
            [InlineKeyboardButton("رجوع للقائمة الرئيسية",callback_data="a:home")]
        ])
        return await edit(q,f"إعدادات البرنامج رقم {pid}\nالاسم الحالي: {p['name']}\nالـ ID الحالي: {p['code']}\nسعر الصرف: 1 دولار = {money_text(float(p['exchange_rate'] or 50))} جنيه مصري\n\nاختر ما تريد تغييره:",kb)
    if d.startswith("pe:name:") or d.startswith("pe:code:") or d.startswith("pe:rate:"):
        _, _, pid = d.split(":")
        pid=int(pid); field="name" if d.startswith("pe:name:") else ("code" if d.startswith("pe:code:") else "rate")
        ctx.user_data["step"]="editprogram_name" if field=="name" else ("editprogram_code" if field=="code" else "editprogram_rate")
        ctx.user_data["pid"]=pid
        prompt="الاسم الجديد" if field=="name" else ("الـ ID الجديد" if field=="code" else "سعر الصرف الجديد بالجنيه لكل دولار")
        return await edit(q,f"أرسل {prompt} للبرنامج رقم {pid}." + ("\nمثال: 50 أو 50.25" if field=="rate" else ""),InlineKeyboardMarkup([
            [InlineKeyboardButton("رجوع لإعدادات البرنامج",callback_data=f"pe:{pid}")],
            [InlineKeyboardButton("رجوع لإدارة البرامج",callback_data="a:programs")],
            [InlineKeyboardButton("إلغاء",callback_data="a:home")]
        ]))
    if d=="a:rate":
        if not is_admin(uid): return await q.answer("غير مصرح.",show_alert=True)
        rate=get_exchange_rate()
        return await edit(q,f"سعر الصرف الحالي: 1 دولار = {money_text(rate)} جنيه مصري.\n\nأرسل السعر الجديد كرقم فقط، أكبر من صفر وبحد أقصى 4 أرقام بعد العلامة العشرية. مثال: 50 أو 50.25",InlineKeyboardMarkup([
            [InlineKeyboardButton("رجوع للقائمة الرئيسية",callback_data="a:home")]
        ]))
    if d=="a:stats":
        with db() as c:
            total=c.execute("SELECT COUNT(*) FROM requests").fetchone()[0]; pending=c.execute("SELECT COUNT(*) FROM requests WHERE status='pending'").fetchone()[0]; ok=c.execute("SELECT COUNT(*) FROM requests WHERE status='approved'").fetchone()[0]; no=c.execute("SELECT COUNT(*) FROM requests WHERE status='rejected'").fetchone()[0]; users=c.execute("SELECT COUNT(DISTINCT user_id) FROM requests").fetchone()[0]
        return await edit(q,f"الإحصائيات\nإجمالي الطلبات: {total}\nمعلقة: {pending}\nمقبولة: {ok}\nمرفوضة: {no}\nمستخدمون: {users}\nالمبالغ مدخلة من المستخدمين وليست إيرادات مؤكدة.",back_kb())
    if d=="a:admins":
        rows=admins(); kb=[]
        for aid in rows:
            kb.append([InlineKeyboardButton(f"👤 {aid}"+(" · المالك" if aid==OWNER else ""),callback_data=f"adminview:{aid}")])
        if owner(uid): kb.append([InlineKeyboardButton("➕ إضافة مشرف",callback_data="adminadd:start")])
        kb.append([InlineKeyboardButton("رجوع",callback_data="a:home")])
        return await edit(q,"إدارة المشرفين",InlineKeyboardMarkup(kb))
    if d=="adminadd:start":
        if not owner(uid): return await q.answer("مالك البوت فقط.",show_alert=True)
        ctx.user_data["step"]="addadmin_id"
        return await edit(q,"أرسل رقم Telegram ID فقط.",InlineKeyboardMarkup([[InlineKeyboardButton("رجوع",callback_data="a:admins")],[InlineKeyboardButton("إلغاء",callback_data="a:admins")]]))
    if d.startswith("adminview:"):
        target=int(d.split(":")[1])
        kb=[]
        if owner(uid) and target!=OWNER: kb.append([InlineKeyboardButton("حذف المشرف",callback_data=f"admindel:{target}")])
        kb.append([InlineKeyboardButton("رجوع",callback_data="a:admins")])
        return await edit(q,f"المشرف: {target}",InlineKeyboardMarkup(kb))
    if d.startswith("admindel:"):
        if not owner(uid): return await q.answer("مالك البوت فقط.",show_alert=True)
        target=int(d.split(":")[1])
        if target==OWNER: return await edit(q,"لا يمكن حذف المالك.",admin_kb())
        with db() as c: c.execute("DELETE FROM admins WHERE user_id=?",(target,))
        audit(uid,"admin_removed",str(target))
        return await edit(q,"تم حذف المشرف.",InlineKeyboardMarkup([[InlineKeyboardButton("رجوع",callback_data="a:admins")]]))
    if d.startswith("a:users"):
        page=int(d.split(":")[2]) if d.count(":")>=2 else 0
        with db() as c:
            rs=c.execute("SELECT u.user_id,COALESCE(u.username,MAX(r.username)) username,COALESCE(u.full_name,MAX(r.full_name)) full_name,COUNT(r.id) n,u.last_seen last_id FROM bot_users u LEFT JOIN requests r ON r.user_id=u.user_id GROUP BY u.user_id ORDER BY u.last_seen DESC LIMIT 10 OFFSET ?",(page*10,)).fetchall()
            total=c.execute("SELECT COUNT(*) FROM bot_users").fetchone()[0]
        kb=[[InlineKeyboardButton(f"{(r['username'] and '@'+r['username']) or (r['full_name'] or str(r['user_id']))} · {r['n']} طلب",callback_data=f"userdetail:{r['user_id']}")] for r in rs]
        nav=[]
        if page>0: nav.append(InlineKeyboardButton("السابق",callback_data=f"a:users:{page-1}"))
        if (page+1)*10<total: nav.append(InlineKeyboardButton("التالي",callback_data=f"a:users:{page+1}"))
        if nav: kb.append(nav)
        kb.append([InlineKeyboardButton("رجوع للقائمة الرئيسية",callback_data="a:home")])
        return await edit(q,f"المستخدمون ({total}) — اختر مستخدمًا",InlineKeyboardMarkup(kb))
    if d.startswith("userdetail:"):
        target=int(d.split(":")[1])
        with db() as c:
            info=c.execute("SELECT user_id,MAX(username) username,MAX(full_name) full_name,COUNT(*) n FROM requests WHERE user_id=? GROUP BY user_id",(target,)).fetchone()
            rs=c.execute("SELECT id,status FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 10",(target,)).fetchall()
            blocked=c.execute("SELECT 1 FROM user_blocks WHERE user_id=?",(target,)).fetchone() is not None
        if not info: return await edit(q,"المستخدم غير موجود.",back_kb("a:users"))
        kb=[[InlineKeyboardButton(f"الطلب #{r['id']} · {LABELS[r['status']]}",callback_data=f"r:{r['id']}")] for r in rs]
        if blocked:
            kb.append([InlineKeyboardButton("رفع الحظر",callback_data=f"userunblock:{target}")])
        else:
            kb.append([InlineKeyboardButton("حظر المستخدم",callback_data=f"userblock:{target}")])
        kb.append([InlineKeyboardButton("رجوع للمستخدمين",callback_data="a:users")])
        title=info["full_name"] or info["username"] or str(target)
        return await edit(q,f"المستخدم: {title}\nTelegram ID: {target}\nعدد الطلبات: {info['n']}\nالحالة: {'محظور' if blocked else 'نشط'}\nاختر إجراءً:",InlineKeyboardMarkup(kb))
    if d.startswith("userblock:") or d.startswith("userunblock:"):
        target=int(d.split(":")[1]); block=d.startswith("userblock:")
        with db() as c:
            if block: c.execute("INSERT OR IGNORE INTO user_blocks(user_id,blocked_by,blocked_at,reason) VALUES(?,?,?,?)",(target,uid,now(),"حظر من لوحة الإدارة"))
            else: c.execute("DELETE FROM user_blocks WHERE user_id=?",(target,))
        audit(uid,"user_blocked" if block else "user_unblocked",str(target))
        try: await ctx.bot.send_message(target,"تم تقييد استخدامك للبوت بواسطة الإدارة." if block else "تم رفع الحظر عن حسابك ويمكنك استخدام البوت مجددًا.")
        except Exception: pass
        return await edit(q,"تم "+("حظر المستخدم." if block else "رفع الحظر عن المستخدم."),InlineKeyboardMarkup([[InlineKeyboardButton("العودة لملف المستخدم",callback_data=f"userdetail:{target}")],[InlineKeyboardButton("قائمة المستخدمين",callback_data="a:users")],[InlineKeyboardButton("القائمة الرئيسية",callback_data="a:home")]]))
    if d=="a:log":
        with db() as c: rs=c.execute("SELECT * FROM activity ORDER BY id DESC LIMIT 15").fetchall()
        return await edit(q,"آخر الأنشطة:\n"+"\n".join(f"#{r['id']} {r['action']} | {r['actor']} | {r['details']}" for r in rs),back_kb())
    if d=="a:export":
        with db() as c: rs=c.execute("SELECT * FROM requests ORDER BY id DESC").fetchall()
        out=io.StringIO(); w=csv.writer(out); w.writerow(["id","user_id","username","program","wallet","amount_claimed","status","created_at","reviewed_at","reviewer_id","reason"])
        for r in rs: w.writerow([csv_safe(v) for v in [r["id"],r["user_id"],r["username"],r["program_name"],r["wallet"],r["amount"],r["status"],r["created_at"],r["reviewed_at"],r["reviewer_id"],r["reason"]]])
        b=io.BytesIO(out.getvalue().encode("utf-8-sig")); b.name="aeh_requests.csv"
        await ctx.bot.send_document(q.message.chat_id,InputFile(b),caption="تقرير الطلبات. المبالغ مدخلة من المستخدمين وليست مبالغ مؤكدة.")
        return await edit(q,"تم تجهيز التقرير.",back_kb())
async def callbacks_proxy_programs(q):
    rows=get_programs(); kb=[]
    for p in rows: kb.append([InlineKeyboardButton(f"{p['id']}. {p['name']} ({'مفعّل' if p['enabled'] else 'متوقف'})",callback_data=f"pe:{p['id']}"),InlineKeyboardButton("إيقاف" if p["enabled"] else "تفعيل",callback_data=f"pt:{p['id']}")])
    kb.append([InlineKeyboardButton("رجوع",callback_data="a:home")])
    await edit(q,"إدارة البرامج — اختر البرنامج، ثم استخدم زر تغيير الاسم أو زر تغيير الـ ID. لكل برنامج ID مستقل.",InlineKeyboardMarkup(kb))
async def messages(u,ctx):
    m=u.effective_message; user=u.effective_user; step=ctx.user_data.get("step")
    if not m or not user: return
    if not u.effective_chat or u.effective_chat.type != "private":
        return await m.reply_text("لحماية رقم المحفظة وصورة التحويل، أرسل البيانات في الخاص فقط.")
    if is_blocked(user.id) and not is_admin(user.id):
        ctx.user_data.clear()
        return await m.reply_text("لا يمكنك استخدام البوت حاليًا. تواصل مع الإدارة.")
    if step=="addadmin_id":
        if not owner(user.id): ctx.user_data.clear(); return
        raw=(m.text or "").strip()
        if not raw.isdigit(): return await m.reply_text("أرسل رقم Telegram ID فقط، بدون أي كلام إضافي.")
        target=int(raw)
        if target<=0: return await m.reply_text("رقم ID غير صحيح. أرسل الرقم فقط.")
        with db() as c: c.execute("INSERT OR IGNORE INTO admins VALUES(?,?,?)",(target,user.id,now()))
        ctx.user_data.clear()
        audit(user.id,"admin_added",str(target))
        return await m.reply_text("تمت إضافة المشرف." if target not in [] else "تمت إضافة المشرف.",reply_markup=admin_kb())
    if getattr(m, "photo", None) and getattr(m, "reply_to_message", None):
        with db() as c: prompt=c.execute("SELECT request_id FROM payout_prompts WHERE admin_id=? AND prompt_message_id=?",(user.id,m.reply_to_message.message_id)).fetchone()
        if prompt:
            rid=prompt["request_id"]
            with db() as c:
                r=c.execute("SELECT * FROM requests WHERE id=?",(rid,)).fetchone()
                if not r or r["status"]!="approved" or r["payout_sent_at"]:
                    c.execute("DELETE FROM payout_prompts WHERE admin_id=? AND prompt_message_id=?",(user.id,m.reply_to_message.message_id))
                    return await m.reply_text("الطلب غير موجود أو تم إرسال إثباته بالفعل.")
                c.execute("INSERT OR REPLACE INTO payout_drafts(request_id,admin_id,photo,created_at) VALUES(?,?,?,?)",(rid,user.id,m.photo[-1].file_id,now()))
                c.execute("DELETE FROM payout_prompts WHERE admin_id=? AND prompt_message_id=?",(user.id,m.reply_to_message.message_id))
            rate=float(r["exchange_rate"]) if r["exchange_rate"] else get_program_rate(r["program_id"])
            egp=float(r["amount"] or 0)*rate; egp_text=money_text(egp)
            caption=(f"معاينة إثبات التحويل — الطلب #{rid}\nالمبلغ الذي سيظهر للمستخدم: {egp_text} جنيه مصري (سعر الصرف: {money_text(rate)} جنيه/دولار)\n\nراجع الصورة والمبلغ. لن يصل شيء للمستخدم إلا بعد الضغط على «إرسال للمستخدم».")
            return await m.reply_photo(m.photo[-1].file_id,caption=caption,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إرسال للمستخدم",callback_data=f"payout_send:{rid}"),InlineKeyboardButton("إلغاء",callback_data=f"payout_cancel:{rid}")]]))
    if step=="payout_photo":
        ctx.user_data.pop("step",None); ctx.user_data.pop("payout_rid",None)
        return await m.reply_text("افتح الطلب المعتمد واضغط «رفع صورة إثبات التحويل»، ثم ردّ على رسالة الطلب بالصورة.")
    if step=="reject":
        if not is_admin(user.id): return
        rid=ctx.user_data.get("rid"); ctx.user_data.clear()
        with db() as c: r=c.execute("SELECT * FROM requests WHERE id=?",(rid,)).fetchone()
        if not r or r["status"]!="pending": return await m.reply_text("الطلب غير موجود أو تم اتخاذ قرار بشأنه.")
        with db() as c: c.execute("UPDATE requests SET status='rejected',reviewed_at=?,reviewer_id=?,reason=? WHERE id=? AND status='pending'",(now(),user.id,(m.text or "بدون سبب")[:500],rid))
        audit(user.id,"request_rejected",str(rid))
        try: await ctx.bot.send_message(r["user_id"],f"تم رفض طلبك #{rid}. السبب: {m.text or 'بدون سبب'}\n\nللاستفسار، تواصل مع خدمة العملاء: 01286591021")
        except Exception: pass
        return await m.reply_text(f"تم رفض الطلب #{rid}.",reply_markup=admin_kb())
    if step=="edit_exchange_rate":
        if not is_admin(user.id):
            ctx.user_data.clear()
            return await m.reply_text("غير مصرح.")
        raw=(m.text or "").strip()
        import re
        if not re.fullmatch(r"(?:[0-9]{1,6})(?:[.][0-9]{1,4})?",raw):
            return await m.reply_text("السعر غير صحيح. أرسل أرقامًا فقط، مع نقطة عشرية اختيارية وبحد أقصى 4 منازل. أمثلة صحيحة: 50 أو 50.25 أو 49.875")
        from decimal import Decimal, InvalidOperation
        try: rate=Decimal(raw)
        except InvalidOperation: rate=Decimal("0")
        if rate<=0 or rate>Decimal("100000"):
            return await m.reply_text("سعر الصرف يجب أن يكون أكبر من صفر ولا يتجاوز 100000 جنيه للدولار.")
        normalized=format(rate,"f")
        with db() as c: c.execute("INSERT INTO settings(key,value) VALUES('exchange_rate',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(normalized,))
        ctx.user_data.clear()
        audit(user.id,"exchange_rate_updated",normalized)
        return await m.reply_text(f"تم تحديث سعر الصرف بنجاح. السعر الجديد: 1 دولار = {money_text(float(rate))} جنيه مصري. سيُستخدم السعر الجديد في كل البرامج والطلبات التي لم يُرسل إثبات دفعها بعد.",reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("تغيير سعر الصرف",callback_data="a:rate")],
            [InlineKeyboardButton("القائمة الرئيسية",callback_data="a:home")]
        ]))
    if step=="editprogram_rate":
        if not is_admin(user.id):
            ctx.user_data.clear()
            return await m.reply_text("غير مصرح.")
        raw=(m.text or "").strip()
        import re
        if not re.fullmatch(r"\d{1,6}(?:\.\d{1,4})?",raw):
            return await m.reply_text("سعر الصرف غير صحيح. أرسل رقمًا موجبًا بحد أقصى 4 منازل عشرية، مثل 50 أو 50.25.")
        from decimal import Decimal, InvalidOperation
        try: rate=Decimal(raw)
        except InvalidOperation: rate=Decimal("0")
        if rate<=0 or rate>Decimal("100000"):
            return await m.reply_text("سعر الصرف يجب أن يكون أكبر من صفر ولا يتجاوز 100000 جنيه للدولار.")
        pid=ctx.user_data.get("pid")
        if not pid: ctx.user_data.clear(); return await m.reply_text("انتهت جلسة التعديل. افتح إدارة البرامج وحاول مرة أخرى.",reply_markup=admin_kb())
        normalized=format(rate,"f")
        with db() as c: c.execute("UPDATE programs SET exchange_rate=? WHERE id=?",(normalized,pid))
        ctx.user_data.clear()
        audit(user.id,"program_exchange_rate_updated",f"{pid}: {normalized}")
        return await m.reply_text(f"تم تحديث سعر الصرف للبرنامج رقم {pid}. السعر الجديد: 1 دولار = {money_text(float(rate))} جنيه مصري.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إعدادات البرنامج",callback_data=f"pe:{pid}")],[InlineKeyboardButton("إدارة البرامج",callback_data="a:programs")],[InlineKeyboardButton("القائمة الرئيسية",callback_data="a:home")]]))
    if step in ("editprogram_code","editprogram_name"):
        if not is_admin(user.id): return
        value=(m.text or "").strip()
        if not value: return await m.reply_text("القيمة لا يمكن أن تكون فارغة. أرسل قيمة صحيحة.")
        pid=ctx.user_data.get("pid")
        if not pid: ctx.user_data.clear(); return await m.reply_text("انتهت جلسة التعديل. افتح إدارة البرامج وحاول مرة أخرى.",reply_markup=admin_kb())
        is_name=step=="editprogram_name"
        value=value[:80] if is_name else value[:200]
        ctx.user_data["pending_program_value"]=value
        ctx.user_data["step"]="confirm_program_edit_name" if is_name else "confirm_program_edit_code"
        label="اسم البرنامج" if is_name else "ID البرنامج"
        prompt=f"هل تريد تأكيد تغيير {label} للبرنامج رقم {pid}؟\\n\\nالقيمة الجديدة: {value}\\n\\nلن يتم الحفظ إلا بعد الضغط على «تأكيد»."
        return await m.reply_text(prompt,reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("تأكيد",callback_data="program_edit_confirm"),InlineKeyboardButton("إلغاء",callback_data="program_edit_cancel")]
        ]))
    if step=="proof":
        if not m.photo:
            return await m.reply_text("الخطوة 1 من 4 — مطلوب اسكرين شوت التارجت. أرسل الصورة كصورة داخل تيليجرام، وليس كملف أو فيديو أو ملصق.")
        if m.photo[-1].file_size and m.photo[-1].file_size>10*1024*1024:
            return await m.reply_text("حجم الصورة أكبر من الحد المسموح (10 ميجابايت). أرسل صورة أصغر وواضحة.")
        ctx.user_data["draft"]["photo"]=m.photo[-1].file_id
        ctx.user_data["step"]="wallet"
        return await m.reply_text("الخطوة 2 من 4 — رقم الكاش\nأرسل رقم المحفظة المصرية المكوّن من 11 رقمًا ويبدأ بـ 01. لا ترسل أي كلمة مرور أو رمز تحقق.")
    if step=="wallet":
        raw=(m.text or "").strip()
        if not raw:
            return await m.reply_text("الخطوة 2 من 4 — أرسل رقم الكاش كنص.")
        if not raw.isascii() or not raw.isdigit() or len(raw)!=11 or not raw.startswith("01"):
            return await m.reply_text("رقم الكاش غير صحيح. يجب أن يتكوّن من 11 رقمًا بالضبط ويبدأ بـ 01. أعد إرسال الرقم فقط.")
        ctx.user_data["draft"]["wallet"]=raw
        ctx.user_data["step"]="customer_id"
        return await m.reply_text("الخطوة 3 من 4 — الـ ID الخاص بك\nأرسل الـ ID الذي تريد تسجيله لهذا الطلب.")
    if step=="customer_id":
        raw=(m.text or "").strip()
        if not raw or len(raw)>100:
            return await m.reply_text("الـ ID مطلوب ويجب ألا يزيد عن 100 حرف. أرسله مرة أخرى.")
        ctx.user_data["draft"]["customer_id"]=raw
        ctx.user_data["step"]="amount"
        return await m.reply_text("الخطوة 4 من 4 — عدد الدولارات\nأرسل عدد الدولارات التي أرسلتها كرقم فقط، مثل: 250 أو 250.50.")
    if step=="amount":
        raw=(m.text or "").strip()
        import re
        if not re.fullmatch(r"\d{1,9}(?:\.\d{1,2})?",raw) or float(raw)<=0:
            return await m.reply_text("عدد الدولارات غير صحيح. أرسل رقمًا أكبر من صفر، ويمكنك استخدام منزلتين عشريتين فقط. مثال: 250 أو 250.50")
        ctx.user_data["draft"]["amount"]=raw
        d=ctx.user_data["draft"]
        with db() as c:
            dup=c.execute("SELECT id FROM requests WHERE wallet=? AND amount=? AND created_at>=? ORDER BY id DESC LIMIT 1",(d["wallet"],d["amount"],datetime.now(timezone.utc).replace(hour=0,minute=0,second=0).isoformat())).fetchone()
            cur=c.execute("INSERT INTO requests(user_id,username,full_name,program_id,program_name,program_code,wallet,customer_id,amount,photo,status,created_at,exchange_rate) VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?)",(user.id,user.username or "",user.full_name,d["pid"],d["name"],d["code"],d["wallet"],d["customer_id"],d["amount"],d["photo"],now(),str(d.get("exchange_rate",50)))); rid=cur.lastrowid
        ctx.user_data.clear(); audit(user.id,"request_submitted",str(rid))
        await m.reply_text(f"تم إرسال البيانات بنجاح.\nرقم الطلب: #{rid}\nالحالة: قيد المراجعة\nستصلك رسالة بعد مراجعة الإدارة. احتفظ برقم الطلب للمتابعة.")
        for aid in admins():
            if dup:
                try: await ctx.bot.send_message(aid,f"تنبيه تكرار محتمل في الطلب #{rid}؛ يطابق المحفظة والمبلغ في الطلب #{dup['id']}.")
                except Exception: pass
            try:
                await ctx.bot.send_photo(aid,d["photo"],caption=f"طلب جديد #{rid}\nالمستخدم: {user.full_name} (@{user.username or 'بدون اسم'})\nTelegram ID: {user.id}\nالبرنامج: {d['name']}\nالكود: {d['code']}\nاسكرين شوت التارجت مرفق\nرقم الكاش: {d['wallet']}\nID العميل: {d['customer_id']}\nعدد الدولارات: {d['amount']}\nتحقق من وصول الأموال بشكل مستقل.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("عرض الطلب",callback_data=f"r:{rid}"),InlineKeyboardButton("قبول",callback_data=f"ok:{rid}"),InlineKeyboardButton("رفض",callback_data=f"no:{rid}")]]))
            except Exception as e: log.exception("Admin notification failed: %s",e)
        return
    if m.text and m.text.startswith("/"): return
    await m.reply_text("أرسل /start لعرض البرامج.")
async def clear_command_menu(app):
    # Clear previously configured commands from the default Telegram bot menu.
    await app.bot.delete_my_commands()
    log.info("Cleared Telegram command menu entries for the default scope.")

async def error_handler(update,ctx): log.exception("Unhandled bot error",exc_info=ctx.error)
def main():
    if not TOKEN: raise RuntimeError("Set BOT_TOKEN in environment variables.")
    if not INITIAL_ADMINS: raise RuntimeError("Set ADMIN_IDS to at least one numeric Telegram ID.")
    init_db()
    app=Application.builder().token(TOKEN).post_init(clear_command_menu).build()
    if OWNER is None or OWNER not in INITIAL_ADMINS:
        raise RuntimeError("Set OWNER_ID to the intended owner Telegram ID and include that ID in ADMIN_IDS.")
    for command,fn in [("start",start),("admin",admin_cmd),("cancel",cancel),("setprogram",setprogram),("addadmin",addadmin),("deladmin",deladmin),("user",usercmd)]:
        app.add_handler(CommandHandler(command,fn))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND,messages))
    app.add_error_handler(error_handler)
    log.info("Aeh bot starting; configured admin count=%s",len(INITIAL_ADMINS))
    app.run_polling(allowed_updates=Update.ALL_TYPES)
if __name__=="__main__": main()
