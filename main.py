import csv, io, logging, os, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aeh")
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_ORDER = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
INITIAL_ADMINS = set(ADMIN_ID_ORDER)
OWNER_RAW = os.getenv("OWNER_ID", "").strip()
OWNER = int(OWNER_RAW) if OWNER_RAW.isdigit() else (ADMIN_ID_ORDER[0] if ADMIN_ID_ORDER else None)
DB_PATH = os.getenv("DB_PATH", "aeh.sqlite3")
LABELS = {"pending": "قيد المراجعة", "approved": "مقبول", "rejected": "مرفوض"}

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def csv_safe(value):
    # Prevent spreadsheet formula execution when exported fields are opened in Excel/Sheets.
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value

def db():
    p = Path(DB_PATH)
    if str(p.parent) not in ("", "."): p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS programs(id INTEGER PRIMARY KEY CHECK(id BETWEEN 1 AND 10),name TEXT NOT NULL,code TEXT NOT NULL DEFAULT '',enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,username TEXT,full_name TEXT,program_id INTEGER,program_name TEXT,program_code TEXT,wallet TEXT,amount TEXT,photo TEXT,status TEXT DEFAULT 'pending',created_at TEXT,reviewed_at TEXT,reviewer_id INTEGER,reason TEXT);
        CREATE INDEX IF NOT EXISTS request_status_idx ON requests(status);
        CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY,added_by INTEGER,added_at TEXT);
        CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY AUTOINCREMENT,actor INTEGER,action TEXT,details TEXT,created_at TEXT);
        """)
        for i in range(1,11):
            c.execute("INSERT OR IGNORE INTO programs VALUES(?,?,?,1)",(i,f"برنامج {i:02d}","لم يحدد الكود بعد"))
        # Seed configured admins only on first database initialization. Do not
        # re-add deleted admins on every restart.
        admin_count = c.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
        if admin_count == 0:
            for aid in INITIAL_ADMINS:
                c.execute("INSERT OR IGNORE INTO admins VALUES(?,?,?)",(aid,aid,now()))
        if OWNER is not None:
            c.execute("INSERT OR IGNORE INTO admins VALUES(?,?,?)",(OWNER,OWNER,now()))

def admins():
    with db() as c: return [r[0] for r in c.execute("SELECT user_id FROM admins")]
def is_admin(uid): return uid in admins()
def owner(uid): return uid == OWNER
def audit(uid, action, details=""):
    with db() as c: c.execute("INSERT INTO activity(actor,action,details,created_at) VALUES(?,?,?,?)",(uid,action,details[:500],now()))
def get_programs(active=False):
    with db() as c: return c.execute("SELECT * FROM programs"+(" WHERE enabled=1" if active else "")+" ORDER BY id").fetchall()
def home_kb(uid=None):
    ps=get_programs(True); rows=[]
    for i in range(0,len(ps),2): rows.append([InlineKeyboardButton(p["name"],callback_data=f"p:{p['id']}") for p in ps[i:i+2]])
    if not rows: rows=[[InlineKeyboardButton("لا توجد برامج متاحة",callback_data="noop")]]
    if uid is not None and is_admin(uid): rows.append([InlineKeyboardButton("لوحة الإدارة",callback_data="a:home")])
    return InlineKeyboardMarkup(rows)
def admin_kb():
    return InlineKeyboardMarkup([
    [InlineKeyboardButton("الطلبات المعلقة",callback_data="a:pending"),InlineKeyboardButton("كل الطلبات",callback_data="a:all")],
    [InlineKeyboardButton("إدارة البرامج",callback_data="a:programs"),InlineKeyboardButton("الإحصائيات",callback_data="a:stats")],
    [InlineKeyboardButton("المستخدمون",callback_data="a:users"),InlineKeyboardButton("المشرفون",callback_data="a:admins")],
    [InlineKeyboardButton("تصدير CSV",callback_data="a:export"),InlineKeyboardButton("سجل النشاط",callback_data="a:log")]])
async def edit(q,text,kb=None):
    try: await q.edit_message_text(text,reply_markup=kb)
    except Exception: await q.message.reply_text(text,reply_markup=kb)

async def start(u,ctx):
    if not u.effective_chat or u.effective_chat.type != "private":
        return await u.effective_message.reply_text("لحماية بياناتك، استخدم البوت في المحادثة الخاصة فقط.")
    ctx.user_data.clear()
    await u.message.reply_text("أهلًا بك. اختر البرنامج المطلوب، ثم أدخل رقم المحفظة والمبلغ وأرسل صورة إثبات التحويل.",reply_markup=home_kb(u.effective_user.id))
async def admin_cmd(u,ctx):
    if not u.effective_chat or u.effective_chat.type != "private":
        return await u.effective_message.reply_text("لوحة الإدارة متاحة في الخاص فقط.")
    if not is_admin(u.effective_user.id): return await u.message.reply_text("هذا القسم للإدارة فقط.")
    await u.message.reply_text("لوحة تحكم الإدارة",reply_markup=admin_kb())
async def cancel(u,ctx):
    if not u.effective_chat or u.effective_chat.type != "private":
        return await u.effective_message.reply_text("استخدم البوت في الخاص فقط لحماية البيانات.")
    ctx.user_data.clear(); await u.message.reply_text("تم الإلغاء. أرسل /start للبدء من جديد.")
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
    text=f"طلب #{rid}\nالحالة: {LABELS[r['status']]}\nالمستخدم: {r['full_name']} (@{r['username'] or 'بدون اسم'})\nTelegram ID: {r['user_id']}\nالبرنامج: {r['program_name']}\nالكود: {r['program_code']}\nالمحفظة: {r['wallet']}\nالمبلغ المُدخل: {r['amount']}\nالتاريخ: {r['created_at']}\nسبب الرفض: {r['reason'] or '—'}\n\nتحقق من وصول التحويل بشكل مستقل."
    buttons=[]
    if r["status"]=="pending": buttons.append([InlineKeyboardButton("قبول",callback_data=f"ok:{rid}"),InlineKeyboardButton("رفض",callback_data=f"no:{rid}")])
    else: buttons.append([InlineKeyboardButton("إعادة للمراجعة",callback_data=f"reopen:{rid}")])
    buttons.append([InlineKeyboardButton("رجوع",callback_data="a:pending")])
    await edit(q,text,InlineKeyboardMarkup(buttons))
    try: await q.message.reply_photo(r["photo"],caption=f"إثبات التحويل للطلب #{rid}")
    except Exception as e: log.warning("Photo display failed: %s",e)
async def decide(q,ctx,rid,status,reason=""):
    uid=q.from_user.id
    with db() as c:
        r=c.execute("SELECT * FROM requests WHERE id=?",(rid,)).fetchone()
        if not r: return await edit(q,"الطلب غير موجود.")
        if r["status"]!="pending": return await edit(q,"تم اتخاذ قرار سابق على هذا الطلب.",admin_kb())
        c.execute("UPDATE requests SET status=?,reviewed_at=?,reviewer_id=?,reason=? WHERE id=? AND status='pending'",(status,now(),uid,reason,rid))
    audit(uid,"request_"+status,f"#{rid} {reason}")
    msg=f"تم قبول طلبك رقم #{rid}." if status=="approved" else f"تم رفض طلبك رقم #{rid}. السبب: {reason}"
    try: await ctx.bot.send_message(r["user_id"],msg)
    except Exception: pass
    await edit(q,f"تم {'قبول' if status=='approved' else 'رفض'} الطلب #{rid} وإبلاغ المستخدم.",admin_kb())
async def callbacks(u,ctx):
    q=u.callback_query; await q.answer(); uid=q.from_user.id; d=q.data
    if not q.message or q.message.chat.type != "private":
        return await q.answer("استخدم البوت في الخاص فقط لحماية البيانات.", show_alert=True)
    if d=="noop": return
    if d.startswith("p:"):
        pid=int(d.split(":")[1])
        with db() as c: p=c.execute("SELECT * FROM programs WHERE id=? AND enabled=1",(pid,)).fetchone()
        if not p: return await edit(q,"البرنامج غير متاح.",home_kb())
        ctx.user_data.clear(); ctx.user_data["draft"]={"pid":pid,"name":p["name"],"code":p["code"]}; ctx.user_data["step"]="wallet"
        return await edit(q,f"البرنامج: {p['name']}\nالكود الخاص به:\n{p['code']}\n\nأرسل رقم المحفظة التي استخدمتها في التحويل. لا ترسل كلمات مرور أو رموز تحقق.",InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء",callback_data="ucancel")]]))
    if d=="ucancel": ctx.user_data.clear(); return await edit(q,"تم الإلغاء.",home_kb())
    if not is_admin(uid): return await q.answer("غير مصرح.",show_alert=True)
    if d=="a:home": return await edit(q,"لوحة تحكم الإدارة",admin_kb())
    if d=="a:pending": return await list_requests(q,"pending")
    if d=="a:all": return await list_requests(q,"all")
    if d.startswith("page:"):
        _,s,p=d.split(":"); return await list_requests(q,s,int(p))
    if d.startswith("r:"): return await request_detail(q,int(d.split(":")[1]))
    if d.startswith("ok:"): return await decide(q,ctx,int(d.split(":")[1]),"approved")
    if d.startswith("no:"):
        ctx.user_data["step"]="reject"; ctx.user_data["rid"]=int(d.split(":")[1])
        return await edit(q,"أرسل سبب الرفض في رسالة واحدة. أرسل /cancel للإلغاء.")
    if d.startswith("reopen:"):
        rid=int(d.split(":")[1])
        with db() as c: c.execute("UPDATE requests SET status='pending',reviewed_at=NULL,reviewer_id=NULL,reason=NULL WHERE id=?",(rid,)); r=c.execute("SELECT user_id FROM requests WHERE id=?",(rid,)).fetchone()
        audit(uid,"request_reopened",str(rid))
        if r:
            try: await ctx.bot.send_message(r["user_id"],f"أعيد طلبك #{rid} للمراجعة.")
            except Exception: pass
        return await request_detail(q,rid)
    if d=="a:programs":
        rows=get_programs(); kb=[]
        for p in rows: kb.append([InlineKeyboardButton(f"{p['id']}. {p['name']} ({'مفعّل' if p['enabled'] else 'متوقف'})",callback_data=f"pe:{p['id']}"),InlineKeyboardButton("إيقاف" if p["enabled"] else "تفعيل",callback_data=f"pt:{p['id']}")])
        kb.append([InlineKeyboardButton("رجوع",callback_data="a:home")])
        return await edit(q,"إدارة البرامج. اضغط الاسم لتعديله، أو استخدم /setprogram رقم | الاسم | الكود.",InlineKeyboardMarkup(kb))
    if d.startswith("pt:"):
        pid=int(d.split(":")[1])
        with db() as c: c.execute("UPDATE programs SET enabled=1-enabled WHERE id=?",(pid,))
        audit(uid,"program_toggle",str(pid)); return await callbacks_proxy_programs(q)
    if d.startswith("pe:"):
        ctx.user_data["step"]="editprogram"; ctx.user_data["pid"]=int(d.split(":")[1])
        return await edit(q,"أرسل الاسم والكود بهذا الشكل: الاسم | الكود")
    if d=="a:stats":
        with db() as c:
            total=c.execute("SELECT COUNT(*) FROM requests").fetchone()[0]; pending=c.execute("SELECT COUNT(*) FROM requests WHERE status='pending'").fetchone()[0]; ok=c.execute("SELECT COUNT(*) FROM requests WHERE status='approved'").fetchone()[0]; no=c.execute("SELECT COUNT(*) FROM requests WHERE status='rejected'").fetchone()[0]; users=c.execute("SELECT COUNT(DISTINCT user_id) FROM requests").fetchone()[0]
        return await edit(q,f"الإحصائيات\nإجمالي الطلبات: {total}\nمعلقة: {pending}\nمقبولة: {ok}\nمرفوضة: {no}\nمستخدمون: {users}\nالمبالغ مدخلة من المستخدمين وليست إيرادات مؤكدة.",admin_kb())
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
        return await edit(q,"أرسل رقم Telegram ID فقط.")
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
    if d=="a:users":
        with db() as c: rs=c.execute("SELECT user_id,MAX(username) username,COUNT(*) n FROM requests GROUP BY user_id ORDER BY MAX(id) DESC LIMIT 20").fetchall()
        kb=[[InlineKeyboardButton(f"{r['username'] and '@'+r['username'] or str(r['user_id'])} · {r['n']} طلب",callback_data=f"userdetail:{r['user_id']}")] for r in rs]
        kb.append([InlineKeyboardButton("رجوع",callback_data="a:home")])
        return await edit(q,"المستخدمون",InlineKeyboardMarkup(kb))
    if d.startswith("userdetail:"):
        target=int(d.split(":")[1])
        with db() as c: rs=c.execute("SELECT id,program_name,amount,status FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 20",(target,)).fetchall()
        kb=[[InlineKeyboardButton(f"#{r['id']} · {LABELS[r['status']]}",callback_data=f"r:{r['id']}")] for r in rs]
        kb.append([InlineKeyboardButton("رجوع للمستخدمين",callback_data="a:users")])
        return await edit(q,f"طلبات المستخدم {target}",InlineKeyboardMarkup(kb))
    if d=="a:log":
        with db() as c: rs=c.execute("SELECT * FROM activity ORDER BY id DESC LIMIT 15").fetchall()
        return await edit(q,"آخر الأنشطة:\n"+"\n".join(f"#{r['id']} {r['action']} | {r['actor']} | {r['details']}" for r in rs),admin_kb())
    if d=="a:export":
        with db() as c: rs=c.execute("SELECT * FROM requests ORDER BY id DESC").fetchall()
        out=io.StringIO(); w=csv.writer(out); w.writerow(["id","user_id","username","program","wallet","amount_claimed","status","created_at","reviewed_at","reviewer_id","reason"])
        for r in rs: w.writerow([csv_safe(v) for v in [r["id"],r["user_id"],r["username"],r["program_name"],r["wallet"],r["amount"],r["status"],r["created_at"],r["reviewed_at"],r["reviewer_id"],r["reason"]]])
        b=io.BytesIO(out.getvalue().encode("utf-8-sig")); b.name="aeh_requests.csv"
        await ctx.bot.send_document(q.message.chat_id,InputFile(b),caption="تقرير الطلبات. المبالغ مدخلة من المستخدمين وليست مبالغ مؤكدة.")
        return await edit(q,"تم تجهيز التقرير.",admin_kb())
async def callbacks_proxy_programs(q):
    rows=get_programs(); kb=[]
    for p in rows: kb.append([InlineKeyboardButton(f"{p['id']}. {p['name']} ({'مفعّل' if p['enabled'] else 'متوقف'})",callback_data=f"pe:{p['id']}"),InlineKeyboardButton("إيقاف" if p["enabled"] else "تفعيل",callback_data=f"pt:{p['id']}")])
    kb.append([InlineKeyboardButton("رجوع",callback_data="a:home")])
    await edit(q,"إدارة البرامج. /setprogram رقم | الاسم | الكود",InlineKeyboardMarkup(kb))
async def messages(u,ctx):
    m=u.effective_message; user=u.effective_user; step=ctx.user_data.get("step")
    if not m or not user: return
    if not u.effective_chat or u.effective_chat.type != "private":
        return await m.reply_text("لحماية رقم المحفظة وصورة التحويل، أرسل البيانات في الخاص فقط.")
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
    if step=="reject":
        if not is_admin(user.id): return
        rid=ctx.user_data.get("rid"); ctx.user_data.clear()
        with db() as c: r=c.execute("SELECT * FROM requests WHERE id=?",(rid,)).fetchone()
        if not r or r["status"]!="pending": return await m.reply_text("الطلب غير موجود أو تم اتخاذ قرار بشأنه.")
        with db() as c: c.execute("UPDATE requests SET status='rejected',reviewed_at=?,reviewer_id=?,reason=? WHERE id=? AND status='pending'",(now(),user.id,(m.text or "بدون سبب")[:500],rid))
        audit(user.id,"request_rejected",str(rid))
        try: await ctx.bot.send_message(r["user_id"],f"تم رفض طلبك #{rid}. السبب: {m.text or 'بدون سبب'}")
        except Exception: pass
        return await m.reply_text(f"تم رفض الطلب #{rid}.",reply_markup=admin_kb())
    if step=="editprogram":
        if not is_admin(user.id): return
        parts=[x.strip() for x in (m.text or "").split("|",1)]
        if len(parts)!=2 or not all(parts): return await m.reply_text("الصيغة: الاسم | الكود")
        pid=ctx.user_data.get("pid"); ctx.user_data.clear()
        with db() as c: c.execute("UPDATE programs SET name=?,code=? WHERE id=?",(parts[0][:80],parts[1][:200],pid))
        audit(user.id,"program_updated",str(pid)); return await m.reply_text("تم تحديث البرنامج.",reply_markup=admin_kb())
    if step=="wallet":
        if not m.text: return await m.reply_text("أرسل رقم المحفظة كنص.")
        if not 4<=len(m.text.strip())<=80: return await m.reply_text("رقم المحفظة غير واضح، أعد إرساله.")
        ctx.user_data["draft"]["wallet"]=m.text.strip(); ctx.user_data["step"]="amount"
        return await m.reply_text("أرسل المبلغ مع العملة (مثال: 25 USD).")
    if step=="amount":
        if not m.text or not any(ch.isdigit() for ch in m.text): return await m.reply_text("أرسل مبلغًا صحيحًا، مثال: 25 USD.")
        ctx.user_data["draft"]["amount"]=m.text.strip()[:50]; ctx.user_data["step"]="proof"
        return await m.reply_text("أرسل صورة واضحة لإثبات التحويل. لا ترسل كلمات مرور أو رموز تحقق.")
    if step=="proof":
        if not m.photo: return await m.reply_text("أرسل الإثبات كصورة.")
        d=ctx.user_data["draft"]
        with db() as c:
            dup=c.execute("SELECT id FROM requests WHERE wallet=? AND amount=? AND created_at>=? ORDER BY id DESC LIMIT 1",(d["wallet"],d["amount"],datetime.now(timezone.utc).replace(hour=0,minute=0,second=0).isoformat())).fetchone()
            cur=c.execute("INSERT INTO requests(user_id,username,full_name,program_id,program_name,program_code,wallet,amount,photo,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,'pending',?)",(user.id,user.username or "",user.full_name,d["pid"],d["name"],d["code"],d["wallet"],d["amount"],m.photo[-1].file_id,now())); rid=cur.lastrowid
        ctx.user_data.clear(); audit(user.id,"request_submitted",str(rid))
        await m.reply_text(f"تم استلام طلبك رقم #{rid}.\nالحالة: قيد المراجعة. ستصلك النتيجة بعد مراجعة الإدارة.")
        for aid in admins():
            if dup:
                try: await ctx.bot.send_message(aid,f"تنبيه تكرار محتمل في الطلب #{rid}؛ يطابق المحفظة والمبلغ في الطلب #{dup['id']}.")
                except Exception: pass
            try:
                await ctx.bot.send_photo(aid,m.photo[-1].file_id,caption=f"طلب جديد #{rid}\nالمستخدم: {user.full_name} (@{user.username or 'بدون اسم'})\nTelegram ID: {user.id}\nالبرنامج: {d['name']}\nالكود: {d['code']}\nالمحفظة: {d['wallet']}\nالمبلغ المُدخل: {d['amount']}\nتحقق من وصول الأموال بشكل مستقل.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("عرض الطلب",callback_data=f"r:{rid}"),InlineKeyboardButton("قبول",callback_data=f"ok:{rid}"),InlineKeyboardButton("رفض",callback_data=f"no:{rid}")]]))
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
