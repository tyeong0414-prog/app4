"""집안일 퀘스트 — streamlit run app.py
Python 3.10+. Streamlit Community Cloud에 배포한 뒤 공개 URL을 QR에 넣으세요.
데이터는 실행 서버의 SQLite 파일에 저장됩니다. 서버 재배포/초기화 시 유실될 수 있습니다.
방 코드는 접근 권한 인증이 아닙니다. 민감한 내용을 입력하지 마세요.
"""
import html
import io
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import qrcode
import streamlit as st

st.set_page_config(page_title="집안일 퀘스트", page_icon="🏠", layout="centered", initial_sidebar_state="collapsed")
DB_PATH = Path(os.environ.get("QUEST_DB_PATH", "house_quests.sqlite3"))
PAGE_SIZE = 3
MAX_OPEN = 60

def connection():
    con = sqlite3.connect(str(DB_PATH), timeout=15, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=15000")
    return con

def initialize():
    with closing(connection()) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""CREATE TABLE IF NOT EXISTS quests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL,
            title TEXT NOT NULL, author TEXT NOT NULL, created_at TEXT NOT NULL,
            done_by TEXT, done_at TEXT)""")
        con.execute("CREATE INDEX IF NOT EXISTS quests_room_idx ON quests(room, id)")
        con.execute("""CREATE TABLE IF NOT EXISTS presence (
            room TEXT NOT NULL, session_id TEXT NOT NULL, name TEXT NOT NULL,
            last_seen REAL NOT NULL, PRIMARY KEY(room, session_id))""")
        con.execute("CREATE INDEX IF NOT EXISTS presence_seen_idx ON presence(room,last_seen)")

initialize()
raw_room = str(st.query_params.get("room", "home"))
room = raw_room if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", raw_room) else "home"
st.session_state.setdefault("nickname", "")
st.session_state.setdefault("page", 0)
st.session_state.setdefault("guest", "가족-" + uuid.uuid4().hex[:4])
st.session_state.setdefault("flash", "")
st.session_state.setdefault("share_url", "")
st.session_state.setdefault("visitor_id", uuid.uuid4().hex)

def esc(value):
    return html.escape(str(value), quote=True)

def now():
    return datetime.now(timezone.utc).isoformat()

def person():
    return st.session_state.nickname.strip()[:16] or st.session_state.guest

def add_quest(title):
    con = connection()
    try:
        con.execute("BEGIN IMMEDIATE")
        count = con.execute("SELECT COUNT(*) FROM quests WHERE room=? AND done_at IS NULL", (room,)).fetchone()[0]
        if count >= MAX_OPEN:
            con.rollback()
            return False
        con.execute("INSERT INTO quests(room,title,author,created_at) VALUES(?,?,?,?)", (room,title,person(),now()))
        con.commit()
        return True
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def complete(quest_id):
    with closing(connection()) as con:
        cur = con.execute("UPDATE quests SET done_by=?, done_at=? WHERE id=? AND room=? AND done_at IS NULL", (person(),now(),quest_id,room))
        return cur.rowcount == 1

def snapshot():
    con = connection()
    try:
        counts = con.execute("SELECT COUNT(*) total, SUM(CASE WHEN done_at IS NULL THEN 1 ELSE 0 END) pending FROM quests WHERE room=?", (room,)).fetchone()
        pending = counts["pending"] or 0
        pages = max(1, (pending + PAGE_SIZE - 1) // PAGE_SIZE)
        st.session_state.page = min(max(0, st.session_state.page), pages - 1)
        rows = con.execute("SELECT * FROM quests WHERE room=? AND done_at IS NULL ORDER BY id DESC LIMIT ? OFFSET ?", (room,PAGE_SIZE,st.session_state.page*PAGE_SIZE)).fetchall()
        last = con.execute("SELECT * FROM quests WHERE room=? AND done_at IS NOT NULL ORDER BY done_at DESC,id DESC LIMIT 1", (room,)).fetchone()
        return pending, counts["total"]-pending, pages, rows, last
    finally:
        con.close()

def heartbeat():
    stamp = datetime.now(timezone.utc).timestamp()
    with closing(connection()) as con:
        con.execute("""INSERT INTO presence(room,session_id,name,last_seen) VALUES(?,?,?,?)
            ON CONFLICT(room,session_id) DO UPDATE SET name=excluded.name,last_seen=excluded.last_seen""",
            (room, st.session_state.visitor_id, person(), stamp))
        con.execute("DELETE FROM presence WHERE last_seen < ?", (stamp - 86400,))
        return con.execute("SELECT name,last_seen FROM presence WHERE room=? AND last_seen>=? ORDER BY name,session_id", (room, stamp - 60)).fetchall()

def history_page(page):
    with closing(connection()) as con:
        total = con.execute("SELECT COUNT(*) FROM quests WHERE room=? AND done_at IS NOT NULL", (room,)).fetchone()[0]
        pages = max(1, (total + 4) // 5)
        page = min(max(1, int(page)), pages)
        rows = con.execute("SELECT * FROM quests WHERE room=? AND done_at IS NOT NULL ORDER BY done_at DESC,id DESC LIMIT 5 OFFSET ?", (room,(page-1)*5)).fetchall()
        return total, pages, rows

def local_time(value):
    return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=9))).strftime("%Y.%m.%d %H:%M")

@st.dialog("✅ 완료 보관함")
def history_dialog():
    total, pages, _ = history_page(1)
    st.caption(f"완료한 퀘스트 {total}개 · 최신순 · 시간은 한국 시간 기준")
    if not total:
        st.info("아직 완료한 퀘스트가 없어요. 첫 집안일을 완료해 보세요!")
        return
    current = min(max(1, int(st.session_state.get("history_page_number", 1))), pages)
    st.session_state.history_page_number = current
    page = st.number_input("기록 페이지", min_value=1, max_value=pages, step=1, key="history_page_number")
    _, _, rows = history_page(page)
    for row in rows:
        st.markdown(f'<div class="history-note"><strong>✓ {esc(row["title"])}</strong><div>{esc(row["done_by"])}님이 완료했습니다.</div><small>요청: {esc(row["author"])} · {local_time(row["done_at"])}</small></div>', unsafe_allow_html=True)
    st.caption(f"{page} / {pages} 페이지 · 기록은 서버에 보관된 전체 완료 내역입니다.")
    st.button("↻ 새로고침", key="history_refresh", use_container_width=True)

@st.dialog("👨‍👩‍👧‍👦 지금 함께하는 가족")
def members_dialog():
    members = heartbeat()
    st.markdown(f"**현재 접속 {len(members)}명**")
    st.caption("최근 60초 이내 연결된 접속 세션 기준입니다. 같은 사람이 다른 기기·탭으로 접속하면 각각 집계되며, 창을 닫으면 최대 약 60초 후 제외됩니다.")
    with st.container(height=240, border=False):
        for member in members:
            st.markdown(f'<div class="member">🟢 {esc(member["name"])}</div>', unsafe_allow_html=True)
    st.button("↻ 인원 새로고침", key="members_refresh", use_container_width=True)

def configured_url():
    if st.session_state.share_url:
        return st.session_state.share_url
    try:
        return str(st.secrets.get("APP_URL", ""))
    except Exception:
        return ""

def room_url(base):
    parsed = urlparse(base.strip())
    if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.username or parsed.password:
        return None
    params = [(k,v) for k,v in parse_qsl(parsed.query, keep_blank_values=True) if k != "room"]
    params.append(("room", room))
    return urlunparse(parsed._replace(query=urlencode(params), fragment=""))

@st.dialog("📱 QR로 가족 초대")
def qr_dialog():
    st.caption("배포 후 주소창의 공개 앱 주소를 붙여 넣으세요. 같은 QR로 들어온 가족은 같은 게시판을 봅니다.")
    base = st.text_input("공개 앱 주소", value=configured_url(), placeholder="https://your-app.streamlit.app", max_chars=500)
    url = room_url(base) if base.strip() else None
    if url:
        st.session_state.share_url = base.strip()
        image = qrcode.make(url)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        left, center, right = st.columns([1,3,1])
        with center:
            st.image(buf.getvalue(), width=220)
        st.link_button("우리 집 게시판 열기", url, use_container_width=True)
        st.download_button("QR 이미지 다운로드", buf.getvalue(), "house-quest-qr.png", "image/png", use_container_width=True)
        st.caption(f"방 코드: {room} · 다른 집은 주소 끝에 ?room=새로운방코드를 붙여 사용하세요.")
    elif base.strip():
        st.error("http:// 또는 https://로 시작하는 올바른 주소를 입력해 주세요.")
    st.caption("QR은 앱 접속용입니다. 앱이 실행되는 공개 서버가 있어야 하며, 방 코드는 비밀번호가 아닙니다.")

@st.dialog("✨ 새 집안일 퀘스트")
def add_dialog():
    with st.form("new_quest", clear_on_submit=True):
        title = st.text_input("어떤 도움이 필요한가요?", max_chars=60, placeholder="예: 빨래를 널어 주세요")
        st.caption(f"요청자: {person()} · 누구나 완료할 수 있어요.")
        submitted = st.form_submit_button("퀘스트 올리기", type="primary", use_container_width=True)
    if submitted:
        title = " ".join(title.split())
        if not title:
            st.warning("할 일을 한 글자 이상 입력해 주세요.")
        elif add_quest(title):
            st.session_state.page = 0
            st.session_state.flash = "새 퀘스트가 등록됐어요!"
            st.rerun()
        else:
            st.warning("진행 중인 퀘스트가 60개입니다. 먼저 기존 퀘스트를 완료해 주세요.")

st.markdown("""<style>
html, body, .stApp {font-family:system-ui,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;color:#334344;}
.stApp {background:repeating-linear-gradient(90deg,#e8eeeb 0,#edf2ee 2px,#e5ece8 4px);}
header[data-testid="stHeader"] {background:transparent;}
.block-container {max-width:510px;padding:1.1rem 1rem .4rem;background:linear-gradient(110deg,#f5f8f4,#dbe6df 48%,#edf2ee);border:2px solid #b8c8bf;border-radius:24px;box-shadow:inset 5px 0 0 #fafffc,inset -6px 0 0 #c2d1c8,0 12px 35px #3b57451c;}
[data-testid="stVerticalBlock"] {gap:.4rem;}
.hero {display:flex;align-items:center;justify-content:space-between;margin:0 0 2px;}
.title {font-size:24px;font-weight:850;letter-spacing:-1px;}
.badge {background:#d46851;color:white;border-radius:7px;padding:5px 8px;font-size:10px;font-weight:800;transform:rotate(4deg);box-shadow:1px 3px 0 #a24d3a;}
.sub {font-size:11px;color:#52655b;margin-bottom:3px;}
.stats {display:flex;gap:8px;margin:2px 0;}
.stat {flex:1;background:#ffffffaa;border:1px solid #baccc1;border-radius:8px;padding:5px 10px;font-size:11px;color:#4d6357;}
.stat b {color:#315b46;font-size:21px;margin-left:7px;}
.empty {text-align:center;padding:34px 8px;background:#fff0ae;box-shadow:2px 4px 0 #78847722;border-radius:2px;color:#665531;font-size:14px;}
.quest-title {font-size:14px;font-weight:750;line-height:1.35;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
.quest-author {font-size:10px;color:#615b45;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.magnet {display:inline-block;width:11px;height:11px;background:radial-gradient(circle at 30% 25%,#f8a68c,#c2533b);border-radius:50%;box-shadow:1px 2px 2px #0004;margin-right:6px;}
[data-testid="stTextInput"] input {min-height:36px;font-size:14px;}
.stButton button, .stDownloadButton button, .stLinkButton a {border-radius:9px;min-height:40px;font-size:12px;border-color:#b2c1b6;background:#f8faf5;color:#334b3c;}
.stButton button[kind="primary"] {background:#3d6952;border-color:#3d6952;color:white;}
[data-testid="stHorizontalBlock"] {gap:.4rem;align-items:center;flex-wrap:nowrap!important;}
[data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {min-width:0!important;}
.st-key-note_0,.st-key-note_1,.st-key-note_2 {position:relative;border:0!important;border-radius:2px!important;box-shadow:2px 4px 0 #586d5524;padding:8px 10px!important;}
.st-key-note_0 {background:linear-gradient(135deg,#fff3b5,#f5df88)!important;}
.st-key-note_1 {background:linear-gradient(135deg,#ffdce0,#f3c5c9)!important;}
.st-key-note_2 {background:linear-gradient(135deg,#d9ecfa,#bfdcec)!important;}
.footer {text-align:center;font-size:10px;color:#52675a;margin-top:2px;}
.history-note {background:#fff1b6;border-left:4px solid #cfaa46;padding:11px 12px;margin:8px 0;border-radius:3px;overflow-wrap:anywhere;font-size:14px;}
.history-note div {font-size:12px;margin:4px 0;color:#47584a;}
.history-note small {font-size:11px;color:#5c6255;}
.member {background:#edf4ee;border-radius:8px;padding:10px;margin:5px 0;overflow-wrap:anywhere;}
[data-testid="stDialog"] [role="dialog"] {background:#f8faf5;}
@media(max-width:600px){
 .block-container {padding:.65rem .7rem .3rem;border-radius:18px;}
 [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {min-width:0!important;}
 .title {font-size:22px;}
}
</style>""", unsafe_allow_html=True)

st.markdown('<div class="hero"><div class="title">🧲 집안일 퀘스트</div><span class="badge">우리 집 냉장고</span></div><div class="sub">오늘의 할 일을 붙이고, 함께 하나씩 떼어내요.</div>', unsafe_allow_html=True)
st.text_input("내 이름", key="nickname", max_chars=16, placeholder="내 이름 (예: 엄마, 민수)", label_visibility="collapsed")

@st.fragment(run_every="3s")
def board():
    members = heartbeat()
    pending, done, pages, rows, _ = snapshot()
    if st.session_state.flash:
        st.toast(st.session_state.flash, icon="✨")
        st.session_state.flash = ""
    st.markdown(f'<div class="stats"><div class="stat">붙인 메모<b>{pending}</b></div><div class="stat">완료한 메모<b>{done}</b></div></div>', unsafe_allow_html=True)
    new, history = st.columns([1,1])
    if new.button("＋ 퀘스트 붙이기", type="primary", use_container_width=True):
        add_dialog()
    if history.button(f"✓ 완료 보관함 · {done}", use_container_width=True):
        history_dialog()
    people, qr = st.columns([1,1])
    if people.button(f"🟢 접속 가족 · {len(members)}명", use_container_width=True):
        members_dialog()
    if qr.button("▦ QR 초대", use_container_width=True):
        qr_dialog()
    if not rows:
        st.markdown('<div class="empty">🎉 냉장고가 깨끗해졌어요!<br>새로운 집안일 메모를 붙여 보세요.</div>', unsafe_allow_html=True)
    for index, row in enumerate(rows):
        with st.container(border=True, key=f"note_{index}"):
            text, action = st.columns([3,1])
            with text:
                st.markdown(f'<div class="quest-title"><span class="magnet"></span>{esc(row["title"])}</div><div class="quest-author">요청 · {esc(row["author"])}</div>', unsafe_allow_html=True)
            with action:
                if st.button("완료 ✓", key=f'done_{row["id"]}', use_container_width=True):
                    success = complete(row["id"])
                    st.session_state.flash = "완료 보관함에 넣었어요!" if success else "다른 가족이 이미 완료했어요."
                    st.rerun(scope="fragment")
    if pages > 1:
        prev, label, nxt = st.columns([1,2,1])
        if prev.button("‹ 이전", disabled=st.session_state.page == 0, use_container_width=True):
            st.session_state.page -= 1
            st.rerun(scope="fragment")
        label.markdown(f'<div class="footer">{st.session_state.page+1} / {pages}</div>', unsafe_allow_html=True)
        if nxt.button("다음 ›", disabled=st.session_state.page >= pages-1, use_container_width=True):
            st.session_state.page += 1
            st.rerun(scope="fragment")
    st.markdown('<div class="footer">메모 3개씩 · 3초마다 갱신 · 지난 기록은 완료 보관함에서</div>', unsafe_allow_html=True)

board()
