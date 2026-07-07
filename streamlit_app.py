"""
🎨 Artist Alley Inventory & Sales Tracker
Full build with password, combo deals, goals w/ costs, series colors,
customer catalog, rate-limit friendly caching, batched writes.
"""

import streamlit as st
import gspread
from gspread.utils import ValueInputOption
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta, date
import pandas as pd
import hmac
import time

st.set_page_config(page_title="Artist Alley", page_icon="🎨", layout="wide")

# ---------- Teal theme polish ----------
st.markdown("""
<style>
    .stButton>button { border-radius: 8px; font-weight: 600; }
    div[data-testid="stMetric"] {
        background-color: #ccfbf1; padding: 12px;
        border-radius: 10px; border-left: 4px solid #0d9488;
    }
    h1, h2, h3 { color: #134e4a; }
</style>
""", unsafe_allow_html=True)

# ================================================================
# 🎪 CUSTOMER VIEW MODE
# ================================================================
query_params = st.query_params
CUSTOMER_MODE = query_params.get("view") == "customer"

# ================================================================
# 🔒 PASSWORD
# ================================================================
def check_password():
    if CUSTOMER_MODE:
        return True

    def password_entered():
        if hmac.compare_digest(st.session_state["password"],
                                st.secrets["app_password"]):
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False

    if st.session_state.get("password_correct", False):
        return True

    st.title("🎨 Artist Alley")
    st.markdown("### 🔒 Enter password to continue")
    st.text_input("Password", type="password",
                  on_change=password_entered, key="password")
    if "password_correct" in st.session_state and not st.session_state["password_correct"]:
        st.error("😕 Incorrect password. Try again.")
    return False

if not check_password():
    st.stop()

# ================================================================
# 📊 GOOGLE SHEETS CONNECTION (with error handling)
# ================================================================
@st.cache_resource
def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open("Artist Alley Tracker")

try:
    sh = get_sheet()
    inv_ws = sh.worksheet("Inventory")
    sales_ws = sh.worksheet("Sales")
    items_ws = sh.worksheet("Sale Items")
except gspread.exceptions.APIError as e:
    if "429" in str(e) or "Quota" in str(e):
        st.error("⏱ **Rate limit reached!** Wait 2 minutes and refresh.")
        st.caption("Google's free API caps at 60 reads/min. "
                   "Your longer cache TTLs will prevent this after next redeploy.")
    else:
        st.error("🚨 Google Sheets connection issue.")
        st.code(str(e))
    if st.button("🔄 Try again"):
        st.cache_data.clear()
        st.cache_resource.clear()
        st.rerun()
    st.stop()
except Exception as e:
    st.error(f"🚨 Unexpected error connecting to Google Sheets.")
    st.code(str(e))
    st.stop()

# Optional tabs
try: deals_ws = sh.worksheet("Combo Deals")
except Exception: deals_ws = None
try: combo_log_ws = sh.worksheet("Combo Sales Log")
except Exception: combo_log_ws = None
try: goals_ws = sh.worksheet("Goals")
except Exception: goals_ws = None
try: colors_ws = sh.worksheet("Series Colors")
except Exception: colors_ws = None

# ================================================================
# 📥 CACHED READS (rate-limit friendly TTLs)
# ================================================================
@st.cache_data(ttl=600)   # 10 min
def get_inventory():
    return inv_ws.get_all_records()

@st.cache_data(ttl=300)   # 5 min
def get_sales():
    return sales_ws.get_all_records()

@st.cache_data(ttl=300)
def get_sale_items():
    return items_ws.get_all_records()

@st.cache_data(ttl=1800)  # 30 min
def get_deals():
    if not deals_ws: return []
    return [d for d in deals_ws.get_all_records()
            if str(d.get("active", "")).upper() == "TRUE"]

@st.cache_data(ttl=1800)
def get_goals():
    if not goals_ws: return []
    try: return goals_ws.get_all_records()
    except Exception: return []

@st.cache_data(ttl=1800)
def get_series_meta():
    if not colors_ws: return {}, {}
    try:
        records = colors_ws.get_all_records()
        colors, icons = {}, {}
        for r in records:
            s = str(r.get("series", "")).strip()
            if not s: continue
            if r.get("color"): colors[s] = str(r["color"]).strip()
            if r.get("icon"): icons[s] = str(r["icon"]).strip()
        return colors, icons
    except Exception:
        return {}, {}

def clear_cache():
    st.cache_data.clear()

def next_id(records):
    if not records: return 1
    return max((int(r.get("id", 0) or 0) for r in records), default=0) + 1

# ================================================================
# 🎨 SERIES COLOR HELPERS
# ================================================================
FALLBACK_PALETTE = [
    "#14b8a6", "#eab308", "#8b5cf6", "#ec4899", "#0ea5e9",
    "#22c55e", "#f97316", "#ef4444", "#a855f7", "#06b6d4",
    "#84cc16", "#f43f5e",
]

def color_for_series(series_name, custom_colors, auto_map):
    if series_name in custom_colors:
        return custom_colors[series_name]
    if series_name not in auto_map:
        idx = abs(hash(series_name)) % len(FALLBACK_PALETTE)
        auto_map[series_name] = FALLBACK_PALETTE[idx]
    return auto_map[series_name]

def contrast_text(hex_color):
    try:
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        brightness = (r*299 + g*587 + b*114) / 1000
        return "#000000" if brightness > 140 else "#ffffff"
    except Exception:
        return "#000000"

# ================================================================
# 🎁 COMBO DEAL LOGIC
# ================================================================
def calculate_combos(cart, inventory, deals):
    if not cart:
        return 0, [], []
    raw_total = sum(c["qty"] * c["price"] for c in cart)
    if not deals:
        return raw_total, [], []

    inv_by_id = {str(i["id"]): i for i in inventory}
    for c in cart:
        inv_item = inv_by_id.get(str(c["item_id"]), {})
        c["category"] = inv_item.get("category", "")

    total_discount = 0
    applied = []
    remaining = {c["item_id"]: c["qty"] for c in cart}
    priority = {"bundle": 0, "qty_same": 1, "bogo": 2,
                "mix_category": 3, "cart_percent": 4}
    deals_sorted = sorted(deals,
        key=lambda d: priority.get(d.get("deal_type", ""), 99))

    for d in deals_sorted:
        dtype = d.get("deal_type", "")
        trigger_qty = int(d.get("trigger_qty", 0) or 0)
        combo_price = float(d.get("combo_price", 0) or 0)
        discount_pct = float(d.get("discount_pct", 0) or 0)

        if dtype == "qty_same":
            cat = d.get("trigger_category", "")
            for c in cart:
                if cat and c["category"] != cat: continue
                available = remaining[c["item_id"]]
                sets = available // trigger_qty if trigger_qty else 0
                if sets > 0:
                    normal = sets * trigger_qty * c["price"]
                    disc = normal - (sets * combo_price)
                    if disc > 0:
                        total_discount += disc
                        applied.append({"deal_id": d["deal_id"],
                            "deal_name": d["deal_name"],
                            "items_qty": sets * trigger_qty,
                            "discount_amount": disc})
                        remaining[c["item_id"]] -= sets * trigger_qty

        elif dtype == "mix_category":
            cat = d.get("trigger_category", "")
            eligible = [c for c in cart
                       if c["category"] == cat and remaining[c["item_id"]] > 0]
            eligible.sort(key=lambda x: -x["price"])
            pool = []
            for c in eligible:
                for _ in range(remaining[c["item_id"]]):
                    pool.append(c)
            sets = len(pool) // trigger_qty if trigger_qty else 0
            for s in range(sets):
                group = pool[s*trigger_qty:(s+1)*trigger_qty]
                normal = sum(g["price"] for g in group)
                disc = normal - combo_price
                if disc > 0:
                    total_discount += disc
                    applied.append({"deal_id": d["deal_id"],
                        "deal_name": d["deal_name"],
                        "items_qty": trigger_qty,
                        "discount_amount": disc})
                    for g in group:
                        remaining[g["item_id"]] -= 1

        elif dtype == "bundle":
            ids = [i.strip() for i in
                   str(d.get("trigger_item_ids", "")).split(",") if i.strip()]
            if not ids: continue
            try:
                counts = [remaining.get(int(i), 0) for i in ids]
            except ValueError:
                continue
            sets = min(counts) if counts else 0
            if sets > 0:
                normal_unit = sum(
                    next((c["price"] for c in cart
                          if str(c["item_id"]) == i), 0) for i in ids)
                disc = (normal_unit * sets) - (sets * combo_price)
                if disc > 0:
                    total_discount += disc
                    applied.append({"deal_id": d["deal_id"],
                        "deal_name": d["deal_name"],
                        "items_qty": sets * len(ids),
                        "discount_amount": disc})
                    for i in ids:
                        remaining[int(i)] -= sets

        elif dtype == "bogo":
            cat = d.get("trigger_category", "")
            eligible = []
            for c in cart:
                if cat and c["category"] != cat: continue
                for _ in range(remaining[c["item_id"]]):
                    eligible.append(c["price"])
            eligible.sort()
            sets = len(eligible) // trigger_qty if trigger_qty else 0
            for s in range(sets):
                group = eligible[s*trigger_qty:(s+1)*trigger_qty]
                disc = group[0] * (discount_pct / 100)
                if disc > 0:
                    total_discount += disc
                    applied.append({"deal_id": d["deal_id"],
                        "deal_name": d["deal_name"],
                        "items_qty": trigger_qty,
                        "discount_amount": disc})

        elif dtype == "cart_percent":
            threshold = combo_price
            if raw_total - total_discount >= threshold:
                disc = (raw_total - total_discount) * (discount_pct / 100)
                if disc > 0:
                    total_discount += disc
                    applied.append({"deal_id": d["deal_id"],
                        "deal_name": d["deal_name"],
                        "items_qty": sum(c["qty"] for c in cart),
                        "discount_amount": disc})

    return raw_total - total_discount, applied, []

# ================================================================
# 🎯 GOALS HELPERS (including event_revenue!)
# ================================================================
def event_revenue(event_name):
    """Total revenue for a specific event so far."""
    sales = get_sales()
    if not sales: return 0
    df = pd.DataFrame(sales)
    df["total"] = pd.to_numeric(df["total"], errors="coerce").fillna(0)
    df["event"] = df.get("event", "").astype(str)
    return df[df["event"] == event_name]["total"].sum()

def get_goal_for_event(event_name):
    goals = get_goals()
    default = {"revenue_goal": 0, "table_cost": 0,
               "hotel_cost": 0, "travel_cost": 0, "other_costs": 0, "notes": ""}

    def parse(g):
        return {
            "revenue_goal": float(g.get("revenue_goal", 0) or 0),
            "table_cost": float(g.get("table_cost", 0) or 0),
            "hotel_cost": float(g.get("hotel_cost", 0) or 0),
            "travel_cost": float(g.get("travel_cost", 0) or 0),
            "other_costs": float(g.get("other_costs", 0) or 0),
            "notes": str(g.get("notes", "") or ""),
        }

    for g in goals:
        if str(g.get("event", "")).strip().lower() == str(event_name).strip().lower():
            return parse(g)
    for g in goals:
        if str(g.get("event", "")).strip().lower() == "_default_":
            return parse(g)
    return default

def render_goal_progress(event_name, compact=False):
    goal = get_goal_for_event(event_name)
    if goal["revenue_goal"] <= 0 and goal["table_cost"] == 0:
        return

    current = event_revenue(event_name)
    total_costs = (goal["table_cost"] + goal["hotel_cost"]
                   + goal["travel_cost"] + goal["other_costs"])
    net_profit = current - total_costs
    pct = min(current / goal["revenue_goal"], 1.0) if goal["revenue_goal"] else 0
    remaining = max(0, goal["revenue_goal"] - current)

    hit_table = goal["table_cost"] > 0 and current >= goal["table_cost"]
    hit_breakeven = total_costs > 0 and current >= total_costs
    hit_half = current >= goal["revenue_goal"] * 0.5
    hit_goal = current >= goal["revenue_goal"]
    hit_stretch = current >= goal["revenue_goal"] * 1.25

    if compact:
        profit_display = (f"💵 +${net_profit:.0f}" if net_profit >= 0
                          else f"⚠️ -${abs(net_profit):.0f}")
        st.markdown(f"**🎯 {event_name}:** "
                    f"${current:.0f} / ${goal['revenue_goal']:.0f}  "
                    f"({pct*100:.0f}%)  •  {profit_display}")
    else:
        st.subheader(f"🎯 Goal Progress — {event_name}")

    st.progress(pct)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Earned", f"${current:.2f}")
    m2.metric("Goal", f"${goal['revenue_goal']:.2f}")
    m3.metric("Remaining", f"${remaining:.2f}" if not hit_goal else "✅ Hit!")
    m4.metric("% of goal", f"{pct*100:.0f}%")

    if not compact and total_costs > 0:
        st.markdown("### 💰 Profit Picture")
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("Total costs", f"${total_costs:.2f}")
        p2.metric("Break-even at", f"${total_costs:.2f}")
        p3.metric("Net profit", f"${net_profit:.2f}",
                  delta_color="normal" if net_profit >= 0 else "inverse")
        margin = (net_profit / current * 100) if current > 0 else 0
        p4.metric("Profit margin", f"{margin:.0f}%")

        with st.expander("📋 Cost breakdown"):
            if goal["table_cost"] > 0: st.write(f"🎪 Table fee: **${goal['table_cost']:.2f}**")
            if goal["hotel_cost"] > 0: st.write(f"🏨 Hotel: **${goal['hotel_cost']:.2f}**")
            if goal["travel_cost"] > 0: st.write(f"✈️ Travel: **${goal['travel_cost']:.2f}**")
            if goal["other_costs"] > 0: st.write(f"🍜 Other: **${goal['other_costs']:.2f}**")
            st.write(f"**Total: ${total_costs:.2f}**")

    if not compact:
        st.markdown("### 🏆 Milestones")
        milestones = []
        if goal["table_cost"] > 0:
            milestones.append((hit_table, f"Table paid off (${goal['table_cost']:.0f})"))
        if total_costs > 0 and total_costs != goal["table_cost"]:
            milestones.append((hit_breakeven, f"💰 BREAK EVEN (${total_costs:.0f})"))
        milestones.append((hit_half, f"Halfway to goal (${goal['revenue_goal']*0.5:.0f})"))
        milestones.append((hit_goal, f"🎉 Goal reached (${goal['revenue_goal']:.0f})"))
        milestones.append((hit_stretch, f"🚀 Stretch 125% (${goal['revenue_goal']*1.25:.0f})"))
        for hit, label in milestones:
            st.markdown(f"- {'✅' if hit else '⬜'} {label}")

    if hit_stretch:
        if st.session_state.get("celebrated_stretch") != event_name:
            st.balloons(); st.session_state["celebrated_stretch"] = event_name
        st.success("🚀 CRUSHING IT — 125%+ of your goal!")
    elif hit_goal:
        if st.session_state.get("celebrated_goal") != event_name:
            st.balloons(); st.session_state["celebrated_goal"] = event_name
        st.success("🎉 Goal reached!")
    elif hit_breakeven and total_costs > 0:
        if st.session_state.get("celebrated_breakeven") != event_name:
            st.balloons(); st.session_state["celebrated_breakeven"] = event_name
        st.success("💰 BROKE EVEN! Every sale from here is profit.")
    elif hit_table and goal["table_cost"] > 0:
        st.info("✅ Table cost covered!")

    if goal["notes"]:
        st.caption(f"📝 {goal['notes']}")

# ================================================================
# 🗂 SESSION STATE
# ================================================================
if "cart" not in st.session_state: st.session_state.cart = []
if "current_event" not in st.session_state: st.session_state.current_event = ""

# ================================================================
# 👀 CUSTOMER VIEW (short-circuits the rest of the app)
# ================================================================
if CUSTOMER_MODE:
    st.markdown("""<style>
        #MainMenu, header, footer {visibility: hidden;}
        [data-testid="stSidebar"] {display: none;}
    </style>""", unsafe_allow_html=True)

    st.title("🎨 Browse the Menu")
    st.caption("Tap the ✨ combo deals badge when ordering!")

    all_items = [i for i in get_inventory() if int(i.get("stock", 0) or 0) > 0]
    if not all_items:
        st.info("No items available right now.")
        st.stop()

    for it in all_items:
        it["category"] = str(it.get("category", "") or "").strip() or "Other"
        it["series"] = str(it.get("series", "") or "").strip() or "Other"
        it["image_url"] = str(it.get("image_url", "") or "").strip()

    cats = sorted(set(i["category"] for i in all_items))
    ser = sorted(set(i["series"] for i in all_items))

    f1, f2 = st.columns(2)
    sel_cat = f1.selectbox("Category", ["🛍 All"] + cats)
    sel_ser = f2.selectbox("Series", ["🌐 All"] + ser)
    search = st.text_input("🔍 Search", "")

    items = all_items
    if sel_cat != "🛍 All": items = [i for i in items if i["category"] == sel_cat]
    if sel_ser != "🌐 All": items = [i for i in items if i["series"] == sel_ser]
    if search.strip():
        s = search.strip().lower()
        items = [i for i in items if s in str(i["name"]).lower()]

    st.markdown(f"**{len(items)} items**")
    st.markdown("---")

    custom_colors, custom_icons = get_series_meta()
    auto_map = {}
    for i in range(0, len(items), 3):
        cols = st.columns(3)
        for col, it in zip(cols, items[i:i+3]):
            with col:
                c = color_for_series(it["series"], custom_colors, auto_map)
                txt = contrast_text(c)
                icon = custom_icons.get(it["series"], "")
                st.markdown(
                    f"<div style='background:{c};color:{txt};padding:6px;"
                    f"border-radius:8px 8px 0 0;text-align:center;font-weight:700;'>"
                    f"{icon} {it['series']}</div>", unsafe_allow_html=True)
                if it["image_url"]:
                    try: st.image(it["image_url"], width="stretch")
                    except Exception: st.markdown("🎨")
                low = int(it["stock"]) <= 3
                stock = "⚠️ Last few!" if low else ""
                st.markdown(
                    f"<div style='text-align:center;padding:6px;"
                    f"background:white;border:1px solid #e2e8f0;'>"
                    f"<b>{it['name']}</b><br>"
                    f"<span style='color:#0d9488;font-size:18px;font-weight:700;'>"
                    f"${float(it['price']):.2f}</span><br>"
                    f"<small style='color:#dc2626;'>{stock}</small></div>",
                    unsafe_allow_html=True)
                st.markdown("<br>", unsafe_allow_html=True)
    st.stop()

# ================================================================
# 📋 SIDEBAR
# ================================================================
st.sidebar.title("🎨 Artist Alley")
page = st.sidebar.radio("Go to", [
    "💵 Quick Sale", "📦 Inventory", "🎁 Combo Deals",
    "🎯 Goals", "📜 Sales History", "📊 Dashboard"
])

st.sidebar.markdown("---")
st.sidebar.markdown("**🏪 Current Event**")
st.session_state.current_event = st.sidebar.text_input(
    "Event", value=st.session_state.current_event,
    placeholder="e.g. Anime North 2026", label_visibility="collapsed")
if st.session_state.current_event:
    st.sidebar.caption(f"Tagging: **{st.session_state.current_event}**")

st.sidebar.markdown("---")
if st.sidebar.button("🔄 Refresh data"):
    clear_cache(); st.rerun()
if st.sidebar.button("🚪 Log out"):
    st.session_state["password_correct"] = False; st.rerun()

# ================================================================
# 💵 QUICK SALE
# ================================================================
if page == "💵 Quick Sale":
    st.title("💵 Quick Sale")
    if st.session_state.current_event:
        st.caption(f"📍 {st.session_state.current_event}")
        render_goal_progress(st.session_state.current_event, compact=True)
        st.markdown("---")

    all_items = [i for i in get_inventory() if int(i.get("stock", 0) or 0) > 0]

    if not all_items:
        st.info("Add items in the Inventory tab first!")
    else:
        for it in all_items:
            it["category"] = str(it.get("category", "") or "").strip() or "Uncategorized"
            it["series"] = str(it.get("series", "") or "").strip() or "Other"

        cat_counts, series_counts = {}, {}
        for it in all_items:
            cat_counts[it["category"]] = cat_counts.get(it["category"], 0) + 1
            series_counts[it["series"]] = series_counts.get(it["series"], 0) + 1
        categories = sorted(cat_counts.keys())
        series_list = sorted(series_counts.keys())

        col1, col2 = st.columns([1.4, 1])

        with col1:
            f1, f2 = st.columns(2)
            cat_options = ["🛍 All"] + [f"{c} ({cat_counts[c]})" for c in categories]
            selected_cat = f1.selectbox("Category", cat_options)
            series_options = ["🌐 All"] + [f"{s} ({series_counts[s]})" for s in series_list]
            selected_series = f2.selectbox("Series", series_options)
            search = st.text_input("🔍 Search", "", placeholder="Type name...",
                                    label_visibility="collapsed")

            items = all_items
            if selected_cat != "🛍 All":
                cat_name = selected_cat.rsplit(" (", 1)[0]
                items = [i for i in items if i["category"] == cat_name]
            if selected_series != "🌐 All":
                series_name = selected_series.rsplit(" (", 1)[0]
                items = [i for i in items if i["series"] == series_name]
            if search.strip():
                s = search.strip().lower()
                items = [i for i in items if s in str(i["name"]).lower()]

            st.caption(f"Showing **{len(items)}** items")

            if not items:
                st.info("No items match.")
            else:
                custom_colors, custom_icons = get_series_meta()
                auto_map = {}

                cols = st.columns(3)
                for i, it in enumerate(items):
                    with cols[i % 3]:
                        c = color_for_series(it["series"], custom_colors, auto_map)
                        txt = contrast_text(c)
                        icon = custom_icons.get(it["series"], "")
                        badge = f"{icon} {it['series']}".strip()
                        st.markdown(
                            f"<div style='background:{c};color:{txt};"
                            f"padding:4px 8px;border-radius:6px 6px 0 0;"
                            f"font-size:11px;font-weight:700;text-align:center;"
                            f"margin-bottom:-4px;'>{badge}</div>",
                            unsafe_allow_html=True)

                        low = int(it["stock"]) <= 3
                        stock_badge = f"⚠️ {it['stock']}" if low else f"stock: {it['stock']}"
                        label = f"**{it['name']}**\n${float(it['price']):.2f} • {stock_badge}"
                        if st.button(label, key=f"add_{it['id']}", width="stretch"):
                            found = False
                            for cc in st.session_state.cart:
                                if cc["item_id"] == it["id"]:
                                    if cc["qty"] + 1 > int(it["stock"]):
                                        st.warning("Not enough stock.")
                                    else:
                                        cc["qty"] += 1
                                    found = True; break
                            if not found:
                                st.session_state.cart.append({
                                    "item_id": it["id"], "name": it["name"],
                                    "qty": 1, "price": float(it["price"]),
                                    "cost": float(it.get("cost", 0) or 0)})
                            st.rerun()
                        st.markdown("<br>", unsafe_allow_html=True)

        with col2:
            st.subheader("🛒 Cart")
            if not st.session_state.cart:
                st.caption("Cart is empty.")
            else:
                raw_total = 0
                for idx, c in enumerate(st.session_state.cart):
                    sub = c["qty"] * c["price"]; raw_total += sub
                    cc = st.columns([3, 1, 1, 1])
                    cc[0].write(f"**{c['name']}**")
                    new_qty = cc[1].number_input("Qty", min_value=1, value=c["qty"],
                        key=f"qty_{idx}", label_visibility="collapsed")
                    if new_qty != c["qty"]:
                        st.session_state.cart[idx]["qty"] = new_qty; st.rerun()
                    cc[2].write(f"${sub:.2f}")
                    if cc[3].button("✕", key=f"rm_{idx}"):
                        st.session_state.cart.pop(idx); st.rerun()

                inventory = get_inventory()
                deals = get_deals()
                final_total, applied_deals, _ = calculate_combos(
                    [dict(c) for c in st.session_state.cart], inventory, deals)

                st.markdown("---")
                st.write(f"Subtotal: ${raw_total:.2f}")

                if applied_deals:
                    st.success("🎉 Combo deals applied!")
                    for d in applied_deals:
                        st.caption(f"✨ **{d['deal_name']}** — save ${d['discount_amount']:.2f}")
                    st.write(f"💰 Total savings: **${raw_total - final_total:.2f}**")

                st.markdown(f"### Total: **${final_total:.2f}**")

                override = st.checkbox("Manually adjust total")
                if override:
                    final_total = st.number_input("Final total ($)",
                        min_value=0.0, value=float(final_total), step=0.50)

                payment = st.selectbox("Payment", ["Cash", "Card", "E-transfer", "Other"])
                notes = st.text_input("Notes (optional)")

                b1, b2 = st.columns(2)
                if b1.button("✅ Complete Sale", type="primary", width="stretch"):
                    # BATCHED WRITES to stay under quota
                    sales = get_sales()
                    sale_id = next_id(sales)
                    sales_ws.append_row([
                        sale_id, datetime.now().isoformat(timespec="seconds"),
                        payment, round(final_total, 2), notes,
                        st.session_state.current_event
                    ], value_input_option=ValueInputOption.user_entered)

                    sale_item_records = get_sale_items()
                    next_item_row_id = next_id(sale_item_records)
                    item_rows = []
                    for c in st.session_state.cart:
                        item_rows.append([next_item_row_id, sale_id, c["item_id"],
                            c["name"], c["qty"], c["price"], c["cost"]])
                        next_item_row_id += 1
                    if item_rows:
                        items_ws.append_rows(item_rows,
                            value_input_option=ValueInputOption.user_entered)

                    # Batch stock updates
                    inv_records = get_inventory()
                    updates = []
                    for c in st.session_state.cart:
                        for idx, inv_item in enumerate(inv_records):
                            if str(inv_item.get("id")) == str(c["item_id"]):
                                new_stock = int(inv_item.get("stock", 0) or 0) - c["qty"]
                                updates.append({"range": f"F{idx + 2}",
                                    "values": [[new_stock]]})
                                break
                    if updates:
                        inv_ws.batch_update(updates,
                            value_input_option=ValueInputOption.user_entered)

                    if combo_log_ws and applied_deals:
                        combo_records = combo_log_ws.get_all_records()
                        next_log_id = next_id(combo_records)
                        combo_rows = []
                        for d in applied_deals:
                            combo_rows.append([next_log_id, sale_id,
                                d["deal_id"], d["deal_name"],
                                d["items_qty"], round(d["discount_amount"], 2)])
                            next_log_id += 1
                        combo_log_ws.append_rows(combo_rows,
                            value_input_option=ValueInputOption.user_entered)

                    clear_cache()
                    st.success(f"Sale #{sale_id} — ${final_total:.2f} 🎉")
                    st.session_state.cart = []; st.rerun()

                if b2.button("🗑 Clear", width="stretch"):
                    st.session_state.cart = []; st.rerun()

# ================================================================
# 📦 INVENTORY
# ================================================================
elif page == "📦 Inventory":
    st.title("📦 Inventory")
    tab1, tab2 = st.tabs(["View", "➕ Add New"])
    with tab1:
        items = get_inventory()
        if items:
            st.dataframe(pd.DataFrame(items), width="stretch", hide_index=True)
            st.caption("💡 Edit in Google Sheet — cached 10 min (refresh with sidebar button)")
        else:
            st.info("No items yet.")
    with tab2:
        with st.form("add_item", clear_on_submit=True):
            name = st.text_input("Name *")
            c1, c2 = st.columns(2)
            category = c1.text_input("Category", placeholder="Charm, Sticker...")
            series = c2.text_input("Series", placeholder="Genshin Impact, Original...")
            c3, c4, c5 = st.columns(3)
            price = c3.number_input("Price ($)", min_value=0.0, step=0.50)
            cost = c4.number_input("Cost ($)", min_value=0.0, step=0.10)
            stock = c5.number_input("Stock", min_value=0, step=1)
            image_url = st.text_input("Image URL (optional)")
            if st.form_submit_button("➕ Add", type="primary"):
                if not name.strip():
                    st.error("Name required.")
                else:
                    new_id = next_id(get_inventory())
                    inv_ws.append_row([new_id, name.strip(), category.strip(),
                        price, cost, int(stock), series.strip(), image_url.strip()],
                        value_input_option=ValueInputOption.user_entered)
                    clear_cache()
                    st.success(f"Added {name}!")

# ================================================================
# 🎁 COMBO DEALS
# ================================================================
elif page == "🎁 Combo Deals":
    st.title("🎁 Combo Deals")
    tab1, tab2, tab3 = st.tabs(["Active", "➕ Add", "📈 Performance"])
    with tab1:
        deals = get_deals() if deals_ws else []
        if deals: st.dataframe(pd.DataFrame(deals), width="stretch", hide_index=True)
        else: st.info("No active deals.")
    with tab2:
        if not deals_ws:
            st.error("Add a 'Combo Deals' tab first.")
        else:
            with st.form("add_deal", clear_on_submit=True):
                name = st.text_input("Deal name *")
                dtype = st.selectbox("Type",
                    ["qty_same", "mix_category", "bundle", "bogo", "cart_percent"])
                c1, c2 = st.columns(2)
                tqty = c1.number_input("Trigger qty", min_value=0, step=1)
                cprice = c2.number_input("Combo price ($)", min_value=0.0, step=0.50)
                c3, c4 = st.columns(2)
                tcat = c3.text_input("Category")
                dpct = c4.number_input("Discount %", min_value=0.0, max_value=100.0)
                titems = st.text_input("Item IDs (bundle)")
                if st.form_submit_button("➕ Add", type="primary"):
                    if not name.strip():
                        st.error("Name required.")
                    else:
                        new_id = next_id(deals_ws.get_all_records())
                        deals_ws.append_row([new_id, name.strip(), dtype,
                            int(tqty), tcat.strip(), titems.strip(),
                            cprice, dpct, "TRUE"],
                            value_input_option=ValueInputOption.user_entered)
                        clear_cache()
                        st.success(f"Added '{name}'!")
    with tab3:
        if combo_log_ws:
            logs = combo_log_ws.get_all_records()
            if logs:
                df = pd.DataFrame(logs)
                df["discount_amount"] = pd.to_numeric(df["discount_amount"],
                    errors="coerce").fillna(0)
                perf = df.groupby("deal_name").agg(
                    times_used=("id", "count"),
                    total_discount=("discount_amount", "sum")).sort_values(
                    "times_used", ascending=False)
                st.dataframe(perf, width="stretch")
                st.bar_chart(perf["times_used"])
            else:
                st.info("No combo sales yet.")

# ================================================================
# 🎯 GOALS
# ================================================================
elif page == "🎯 Goals":
    st.title("🎯 Sales Goals")
    tab1, tab2, tab3 = st.tabs(["Current Event", "All Goals", "📈 History"])
    with tab1:
        if not st.session_state.current_event:
            st.info("👈 Set a Current Event in sidebar first.")
        else:
            render_goal_progress(st.session_state.current_event, compact=False)
            goal = get_goal_for_event(st.session_state.current_event)
            if goal["revenue_goal"] > 0:
                current = event_revenue(st.session_state.current_event)
                total_costs = (goal["table_cost"] + goal["hotel_cost"]
                              + goal["travel_cost"] + goal["other_costs"])
                st.markdown("---")
                st.subheader("⏱ Pace Calculator")
                pc1, pc2 = st.columns(2)
                hw = pc1.number_input("Hours worked so far", min_value=0.5, value=4.0, step=0.5)
                hr = pc2.number_input("Hours remaining", min_value=0.0, value=6.0, step=0.5)
                if hw > 0:
                    rate = current / hw
                    projected = current + (rate * hr)
                    projected_profit = projected - total_costs
                    m1, m2, m3 = st.columns(3)
                    m1.metric("Rate", f"${rate:.2f}/hr")
                    m2.metric("Projected revenue", f"${projected:.2f}")
                    m3.metric("Projected profit", f"${projected_profit:.2f}",
                        delta_color="normal" if projected_profit >= 0 else "inverse")
                    breakeven_needed = total_costs - current
                    if breakeven_needed > 0 and hr > 0:
                        st.warning(f"⚠️ Still ${breakeven_needed:.0f} to break even. "
                                   f"Need ${breakeven_needed/hr:.2f}/hr just to cover costs.")
                    remaining = goal["revenue_goal"] - current
                    if remaining > 0 and hr > 0:
                        need = remaining / hr
                        if need > rate:
                            st.warning(f"💪 Need ${need:.2f}/hr to hit goal.")
                        else:
                            st.success(f"✅ On track! Just need ${need:.2f}/hr.")

    with tab2:
        goals = get_goals()
        if not goals:
            st.info("Add goals to 'Goals' tab.")
        else:
            st.dataframe(pd.DataFrame(goals), width="stretch", hide_index=True)

    with tab3:
        goals = get_goals()
        sales = get_sales()
        if not goals or not sales:
            st.info("Need goals + sales data.")
        else:
            df_sales = pd.DataFrame(sales)
            df_sales["total"] = pd.to_numeric(df_sales["total"], errors="coerce").fillna(0)
            df_sales["event"] = df_sales.get("event", "").astype(str)
            history = []
            for g in goals:
                event = str(g.get("event", "")).strip()
                if event == "_default_" or not event: continue
                gval = float(g.get("revenue_goal", 0) or 0)
                if gval <= 0: continue
                tcost = (float(g.get("table_cost", 0) or 0)
                        + float(g.get("hotel_cost", 0) or 0)
                        + float(g.get("travel_cost", 0) or 0)
                        + float(g.get("other_costs", 0) or 0))
                actual = df_sales[df_sales["event"] == event]["total"].sum()
                pct = (actual/gval*100) if gval else 0
                profit = actual - tcost
                margin = (profit/actual*100) if actual > 0 else 0
                history.append({"Event": event, "Revenue": actual, "Goal": gval,
                    "Total Cost": tcost, "Net Profit": profit, "Margin": margin,
                    "% of Goal": pct,
                    "Result": "🎉 Hit" if pct>=100 else ("👍 Close" if pct>=80 else "📉 Missed"),
                    "Profitable": "✅" if profit > 0 else "❌"})
            if history:
                df_hist = pd.DataFrame(history).sort_values("Net Profit", ascending=False)
                hit = sum(1 for h in history if h["% of Goal"] >= 100)
                prof = sum(1 for h in history if h["Net Profit"] > 0)
                total_p = sum(h["Net Profit"] for h in history)
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Events", len(history))
                m2.metric("Goals hit", f"{hit} ({hit/len(history)*100:.0f}%)")
                m3.metric("Profitable", f"{prof} ({prof/len(history)*100:.0f}%)")
                m4.metric("Total profit", f"${total_p:.2f}")
                disp = df_hist.copy()
                for col in ["Revenue", "Goal", "Total Cost"]:
                    disp[col] = disp[col].map("${:.0f}".format)
                disp["Net Profit"] = disp["Net Profit"].map("${:+.0f}".format)
                disp["Margin"] = disp["Margin"].map("{:.0f}%".format)
                disp["% of Goal"] = disp["% of Goal"].map("{:.0f}%".format)
                st.dataframe(disp, width="stretch", hide_index=True)
                st.subheader("💰 Net Profit by Convention")
                st.bar_chart(df_hist.set_index("Event")["Net Profit"])

# ================================================================
# 📜 SALES HISTORY
# ================================================================
elif page == "📜 Sales History":
    st.title("📜 Sales History")
    sales = get_sales()
    if not sales:
        st.info("No sales yet.")
    else:
        df = pd.DataFrame(sales)
        events = sorted(set(str(e) for e in df.get("event", []) if e))
        ef = st.selectbox("Filter by event", ["All"] + events)
        if ef != "All": df = df[df["event"] == ef]
        df = df.sort_values("sold_at", ascending=False)
        st.dataframe(df, width="stretch", hide_index=True)
        total = pd.to_numeric(df["total"], errors="coerce").sum()
        st.caption(f"Showing {len(df)} sales • Total: **${total:.2f}**")

# ================================================================
# 📊 DASHBOARD
# ================================================================
elif page == "📊 Dashboard":
    st.title("📊 Dashboard")
    if st.session_state.current_event:
        goal = get_goal_for_event(st.session_state.current_event)
        if goal["revenue_goal"] > 0:
            render_goal_progress(st.session_state.current_event, compact=True)
            st.markdown("---")

    sales = get_sales()
    sale_items = get_sale_items()
    if not sales:
        st.info("No sales data yet.")
    else:
        df_sales = pd.DataFrame(sales)
        df_sales["total"] = pd.to_numeric(df_sales["total"], errors="coerce").fillna(0)
        df_sales["sold_at"] = pd.to_datetime(df_sales["sold_at"], errors="coerce")
        df_sales["event"] = df_sales["event"].fillna("(no event)").replace("", "(no event)")

        today = pd.Timestamp(date.today())
        week_ago = today - pd.Timedelta(days=7)
        rev_t = df_sales[df_sales["sold_at"] >= today]["total"].sum()
        rev_w = df_sales[df_sales["sold_at"] >= week_ago]["total"].sum()
        rev_a = df_sales["total"].sum()

        df_items = pd.DataFrame(sale_items) if sale_items else pd.DataFrame()
        profit = 0
        if not df_items.empty:
            for col in ["unit_price", "unit_cost", "qty"]:
                df_items[col] = pd.to_numeric(df_items[col], errors="coerce").fillna(0)
            profit = ((df_items["unit_price"] - df_items["unit_cost"]) * df_items["qty"]).sum()

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Today", f"${rev_t:.2f}")
        m2.metric("7 days", f"${rev_w:.2f}")
        m3.metric("All-time", f"${rev_a:.2f}")
        m4.metric("Profit", f"${profit:.2f}")

        st.markdown("---")
        st.subheader("🏪 Revenue by Convention")
        by_event = df_sales.groupby("event").agg(
            revenue=("total", "sum"),
            sales_count=("id", "count")).sort_values("revenue", ascending=False)
        st.bar_chart(by_event["revenue"])
        st.dataframe(by_event, width="stretch")

        if not df_items.empty:
            st.subheader("🏆 Top Sellers")
            top = df_items.groupby("item_name")["qty"].sum().sort_values(
                ascending=False).head(10)
            st.bar_chart(top)

        st.subheader("📅 Daily Revenue")
        daily = df_sales.groupby(df_sales["sold_at"].dt.date)["total"].sum()
        st.line_chart(daily)

    inv = get_inventory()
    if inv:
        df_inv = pd.DataFrame(inv)
        df_inv.columns = [str(c).strip().lower() for c in df_inv.columns]
        if "stock" in df_inv.columns and "name" in df_inv.columns:
            df_inv["stock"] = pd.to_numeric(df_inv["stock"], errors="coerce").fillna(0)
            low = df_inv[df_inv["stock"] <= 3]
            if not low.empty:
                st.subheader("⚠️ Low Stock")
                st.dataframe(low[["name", "stock"]], hide_index=True, width="stretch")