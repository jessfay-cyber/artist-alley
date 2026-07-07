"""
🎨 Artist Alley Inventory & Sales Tracker
Streamlit app with Google Sheets backend, password protection,
combo deal auto-pricing, and per-convention revenue tracking.
"""

import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta, date
import pandas as pd
import hmac

st.set_page_config(page_title="Artist Alley", page_icon="🎨", layout="wide")
# ---------- Custom teal styling ----------
st.markdown("""
<style>
    /* Primary buttons */
    .stButton>button {
        background-color: #0d9488;
        color: white;
        border: none;
        border-radius: 8px;
        font-weight: 600;
        transition: all 0.2s ease;
    }
    .stButton>button:hover {
        background-color: #0f766e;
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(13, 148, 136, 0.25);
    }

    /* Metric cards */
    div[data-testid="stMetric"] {
        background-color: #ccfbf1;
        padding: 16px;
        border-radius: 12px;
        border-left: 4px solid #0d9488;
    }
    div[data-testid="stMetricValue"] {
        color: #134e4a;
        font-weight: 700;
    }

    /* Success messages */
    div[data-testid="stAlert"][data-baseweb="notification"] {
        border-radius: 10px;
    }

    /* Sidebar accent */
    section[data-testid="stSidebar"] {
        border-right: 2px solid #5eead4;
    }

    /* Headers */
    h1, h2, h3 {
        color: #134e4a;
    }

    /* Tab styling */
    button[data-baseweb="tab"] {
        color: #0f766e;
    }
    button[data-baseweb="tab"][aria-selected="true"] {
        color: #0d9488;
        border-bottom-color: #0d9488 !important;
    }
</style>
""", unsafe_allow_html=True)

# ================================================================
# 🔒 PASSWORD PROTECTION
# ================================================================
def check_password():
    """Returns True if user entered the correct password."""
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
# 📊 GOOGLE SHEETS CONNECTION
# ================================================================
@st.cache_resource
def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open("Artist Alley Tracker")

sh = get_sheet()
inv_ws = sh.worksheet("Inventory")
sales_ws = sh.worksheet("Sales")
items_ws = sh.worksheet("Sale Items")

# Optional combo deal tabs (won't crash if not yet created)
try:
    deals_ws = sh.worksheet("Combo Deals")
    combo_log_ws = sh.worksheet("Combo Sales Log")
except Exception:
    deals_ws = None
    combo_log_ws = None

# ================================================================
# 📥 CACHED READS
# ================================================================
@st.cache_data(ttl=10)
def get_inventory():
    return inv_ws.get_all_records()

@st.cache_data(ttl=10)
def get_sales():
    return sales_ws.get_all_records()

@st.cache_data(ttl=10)
def get_sale_items():
    return items_ws.get_all_records()

@st.cache_data(ttl=30)
def get_deals():
    if not deals_ws: return []
    return [d for d in deals_ws.get_all_records()
            if str(d.get("active", "")).upper() == "TRUE"]

# ================================================================
# 🔧 UTILITIES
# ================================================================
def clear_cache():
    st.cache_data.clear()

def next_id(records):
    if not records: return 1
    return max((int(r.get("id", 0) or 0) for r in records), default=0) + 1

# ================================================================
# 🎁 COMBO DEAL LOGIC
# ================================================================
def calculate_combos(cart, inventory, deals):
    """
    Given a cart and active deals, return:
    - adjusted_total: final price after all combos
    - applied_deals: list of {deal_id, deal_name, discount_amount, items_qty}
    - line_details: reserved for future receipt display
    """
    if not cart:
        return 0, [], []

    raw_total = sum(c["qty"] * c["price"] for c in cart)

    if not deals:
        return raw_total, [], []

    # Attach categories from inventory
    inv_by_id = {str(i["id"]): i for i in inventory}
    for c in cart:
        inv_item = inv_by_id.get(str(c["item_id"]), {})
        c["category"] = inv_item.get("category", "")

    total_discount = 0
    applied = []
    remaining = {c["item_id"]: c["qty"] for c in cart}

    # Priority: specific bundles first, then category/qty, then cart-wide
    priority = {"bundle": 0, "qty_same": 1, "bogo": 2,
                "mix_category": 3, "cart_percent": 4}
    deals_sorted = sorted(deals,
        key=lambda d: priority.get(d.get("deal_type", ""), 99))

    for d in deals_sorted:
        dtype = d.get("deal_type", "")
        trigger_qty = int(d.get("trigger_qty", 0) or 0)
        combo_price = float(d.get("combo_price", 0) or 0)
        discount_pct = float(d.get("discount_pct", 0) or 0)

        # ---- Same-item quantity discount ----
        if dtype == "qty_same":
            cat = d.get("trigger_category", "")
            for c in cart:
                if cat and c["category"] != cat: continue
                available = remaining[c["item_id"]]
                sets = available // trigger_qty if trigger_qty else 0
                if sets > 0:
                    normal = sets * trigger_qty * c["price"]
                    discounted = sets * combo_price
                    disc = normal - discounted
                    if disc > 0:
                        total_discount += disc
                        applied.append({
                            "deal_id": d["deal_id"],
                            "deal_name": d["deal_name"],
                            "items_qty": sets * trigger_qty,
                            "discount_amount": disc
                        })
                        remaining[c["item_id"]] -= sets * trigger_qty

        # ---- Mix & match category ----
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
                    applied.append({
                        "deal_id": d["deal_id"],
                        "deal_name": d["deal_name"],
                        "items_qty": trigger_qty,
                        "discount_amount": disc
                    })
                    for g in group:
                        remaining[g["item_id"]] -= 1

        # ---- Bundle (specific item IDs) ----
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
                normal = normal_unit * sets
                discounted = sets * combo_price
                disc = normal - discounted
                if disc > 0:
                    total_discount += disc
                    applied.append({
                        "deal_id": d["deal_id"],
                        "deal_name": d["deal_name"],
                        "items_qty": sets * len(ids),
                        "discount_amount": disc
                    })
                    for i in ids:
                        remaining[int(i)] -= sets

        # ---- BOGO (cheapest free per group of trigger_qty) ----
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
                    applied.append({
                        "deal_id": d["deal_id"],
                        "deal_name": d["deal_name"],
                        "items_qty": trigger_qty,
                        "discount_amount": disc
                    })

        # ---- Cart-wide percentage ----
        elif dtype == "cart_percent":
            threshold = combo_price
            if raw_total - total_discount >= threshold:
                disc = (raw_total - total_discount) * (discount_pct / 100)
                if disc > 0:
                    total_discount += disc
                    applied.append({
                        "deal_id": d["deal_id"],
                        "deal_name": d["deal_name"],
                        "items_qty": sum(c["qty"] for c in cart),
                        "discount_amount": disc
                    })

    return raw_total - total_discount, applied, []

# ================================================================
# 🗂 SESSION STATE
# ================================================================
if "cart" not in st.session_state:
    st.session_state.cart = []
if "current_event" not in st.session_state:
    st.session_state.current_event = ""

# ================================================================
# 📋 SIDEBAR
# ================================================================
st.sidebar.title("🎨 Artist Alley")
page = st.sidebar.radio("Go to", [
    "💵 Quick Sale",
    "📦 Inventory",
    "🎁 Combo Deals",
    "📜 Sales History",
    "📊 Dashboard"
])

st.sidebar.markdown("---")
st.sidebar.markdown("**🏪 Current Event**")
st.session_state.current_event = st.sidebar.text_input(
    "Set once per con",
    value=st.session_state.current_event,
    placeholder="e.g. Anime North 2026",
    label_visibility="collapsed")
if st.session_state.current_event:
    st.sidebar.caption(f"Tagging sales as: **{st.session_state.current_event}**")

st.sidebar.markdown("---")
if st.sidebar.button("🔄 Refresh data"):
    clear_cache(); st.rerun()
if st.sidebar.button("🚪 Log out"):
    st.session_state["password_correct"] = False
    st.rerun()

# ================================================================
# 💵 QUICK SALE
# ================================================================
if page == "💵 Quick Sale":
    st.title("💵 Quick Sale")
    if st.session_state.current_event:
        st.caption(f"📍 Current event: **{st.session_state.current_event}**")

    all_items = [i for i in get_inventory() if int(i.get("stock", 0) or 0) > 0]

    if not all_items:
        st.info("Add items in the Inventory tab first!")
    else:
        # Normalize category values
        for it in all_items:
            it["category"] = str(it.get("category", "") or "").strip() or "Uncategorized"

        # Get unique categories, sorted with item counts
        cat_counts = {}
        for it in all_items:
            cat_counts[it["category"]] = cat_counts.get(it["category"], 0) + 1
        categories = sorted(cat_counts.keys())

        # Get top sellers for the ⭐ shortcut view
        try:
            sale_items_data = get_sale_items()
            if sale_items_data:
                df_si = pd.DataFrame(sale_items_data)
                df_si["qty"] = pd.to_numeric(df_si["qty"], errors="coerce").fillna(0)
                top_ids = (df_si.groupby("item_id")["qty"].sum()
                           .sort_values(ascending=False).head(12).index.tolist())
                top_ids = [str(t) for t in top_ids]
            else:
                top_ids = []
        except Exception:
            top_ids = []

        col1, col2 = st.columns([1.4, 1])

        with col1:
            # ---------- Filter bar ----------
            filter_options = ["🛍 All Items"]
            if top_ids:
                filter_options.append("⭐ Top Sellers")
            filter_options += [f"{c} ({cat_counts[c]})" for c in categories]

            selected_filter = st.radio(
                "Category", filter_options,
                horizontal=True, label_visibility="collapsed")

            # Live search
            search = st.text_input(
                "🔍 Search", "",
                placeholder="Type a name to filter...",
                label_visibility="collapsed")

            # Apply filters
            if selected_filter == "🛍 All Items":
                items = all_items
            elif selected_filter == "⭐ Top Sellers":
                items = [i for i in all_items if str(i["id"]) in top_ids]
                # Preserve top-seller order
                items.sort(key=lambda x: top_ids.index(str(x["id"]))
                           if str(x["id"]) in top_ids else 999)
            else:
                # Strip the "(count)" suffix
                cat_name = selected_filter.rsplit(" (", 1)[0]
                items = [i for i in all_items if i["category"] == cat_name]

            if search.strip():
                s = search.strip().lower()
                items = [i for i in items if s in str(i["name"]).lower()]

            st.caption(f"Showing **{len(items)}** item(s)")

            # ---------- Item grid ----------
            if not items:
                st.info("No items match your filter/search.")
            else:
                # 3-column compact grid
                cols = st.columns(3)
                for i, it in enumerate(items):
                    with cols[i % 3]:
                        low = int(it["stock"]) <= 3
                        stock_badge = f"⚠️ {it['stock']}" if low else f"stock: {it['stock']}"
                        label = (f"**{it['name']}**\n"
                                 f"${float(it['price']):.2f}\n"
                                 f"{stock_badge}")
                        if st.button(label, key=f"add_{it['id']}",
                                     use_container_width=True):
                            found = False
                            for c in st.session_state.cart:
                                if c["item_id"] == it["id"]:
                                    if c["qty"] + 1 > int(it["stock"]):
                                        st.warning("Not enough stock.")
                                    else:
                                        c["qty"] += 1
                                    found = True
                                    break
                            if not found:
                                st.session_state.cart.append({
                                    "item_id": it["id"],
                                    "name": it["name"],
                                    "qty": 1,
                                    "price": float(it["price"]),
                                    "cost": float(it.get("cost", 0) or 0)
                                })
                            st.rerun()

        with col2:
            st.subheader("🛒 Cart")
            if not st.session_state.cart:
                st.caption("Cart is empty.")
            else:
                raw_total = 0
                for idx, c in enumerate(st.session_state.cart):
                    sub = c["qty"] * c["price"]
                    raw_total += sub
                    cc = st.columns([3, 1, 1, 1])
                    cc[0].write(f"**{c['name']}**")
                    new_qty = cc[1].number_input(
                        "Qty", min_value=1, value=c["qty"],
                        key=f"qty_{idx}", label_visibility="collapsed")
                    if new_qty != c["qty"]:
                        st.session_state.cart[idx]["qty"] = new_qty
                        st.rerun()
                    cc[2].write(f"${sub:.2f}")
                    if cc[3].button("✕", key=f"rm_{idx}"):
                        st.session_state.cart.pop(idx)
                        st.rerun()

                # Combo calculation
                inventory = get_inventory()
                deals = get_deals()
                final_total, applied_deals, _ = calculate_combos(
                    [dict(c) for c in st.session_state.cart],
                    inventory, deals)

                st.markdown("---")
                st.write(f"Subtotal: ${raw_total:.2f}")

                if applied_deals:
                    st.success("🎉 Combo deals applied!")
                    for d in applied_deals:
                        st.caption(f"✨ **{d['deal_name']}** — save "
                                   f"${d['discount_amount']:.2f}")
                    savings = raw_total - final_total
                    st.write(f"💰 Total savings: **${savings:.2f}**")

                st.markdown(f"### Total: **${final_total:.2f}**")

                override = st.checkbox("Manually adjust total")
                if override:
                    final_total = st.number_input(
                        "Final total ($)", min_value=0.0,
                        value=float(final_total), step=0.50)

                payment = st.selectbox("Payment",
                    ["Cash", "Card", "E-transfer", "Other"])
                notes = st.text_input("Notes (optional)")

                b1, b2 = st.columns(2)
                if b1.button("✅ Complete Sale", type="primary",
                             use_container_width=True):
                    sales = get_sales()
                    sale_id = next_id(sales)
                    sales_ws.append_row([
                        sale_id,
                        datetime.now().isoformat(timespec="seconds"),
                        payment,
                        round(final_total, 2),
                        notes,
                        st.session_state.current_event
                    ])

                    sale_item_records = get_sale_items()
                    next_item_row_id = next_id(sale_item_records)

                    for c in st.session_state.cart:
                        items_ws.append_row([
                            next_item_row_id, sale_id, c["item_id"],
                            c["name"], c["qty"], c["price"], c["cost"]
                        ])
                        next_item_row_id += 1

                        cell = inv_ws.find(str(c["item_id"]), in_column=1)
                        if cell:
                            current = int(inv_ws.cell(cell.row, 6).value or 0)
                            inv_ws.update_cell(cell.row, 6,
                                               current - c["qty"])

                    if combo_log_ws and applied_deals:
                        combo_records = combo_log_ws.get_all_records()
                        next_log_id = next_id(combo_records)
                        for d in applied_deals:
                            combo_log_ws.append_row([
                                next_log_id, sale_id,
                                d["deal_id"], d["deal_name"],
                                d["items_qty"],
                                round(d["discount_amount"], 2)
                            ])
                            next_log_id += 1

                    clear_cache()
                    st.success(f"Sale #{sale_id} recorded — "
                               f"${final_total:.2f} 🎉")
                    st.session_state.cart = []
                    st.rerun()

                if b2.button("🗑 Clear Cart", use_container_width=True):
                    st.session_state.cart = []
                    st.rerun()
# ================================================================
# 📦 INVENTORY
# ================================================================
elif page == "📦 Inventory":
    st.title("📦 Inventory")
    tab1, tab2 = st.tabs(["View", "➕ Add New"])

    with tab1:
        items = get_inventory()
        if items:
            df = pd.DataFrame(items)
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.caption("💡 Edit directly in your Google Sheet — "
                       "changes appear within 10 sec (or hit Refresh).")
        else:
            st.info("No items yet.")

    with tab2:
        with st.form("add_item", clear_on_submit=True):
            name = st.text_input("Name *")
            category = st.text_input("Category",
                placeholder="Print, Sticker, Charm, etc.")
            c1, c2, c3 = st.columns(3)
            price = c1.number_input("Price ($)", min_value=0.0, step=0.50)
            cost = c2.number_input("Cost ($)", min_value=0.0, step=0.10)
            stock = c3.number_input("Stock", min_value=0, step=1)
            if st.form_submit_button("➕ Add", type="primary"):
                if not name.strip():
                    st.error("Name required.")
                else:
                    new_id = next_id(get_inventory())
                    inv_ws.append_row([
                        new_id, name.strip(), category.strip(),
                        price, cost, int(stock)
                    ])
                    clear_cache()
                    st.success(f"Added {name}!")

# ================================================================
# 🎁 COMBO DEALS
# ================================================================
elif page == "🎁 Combo Deals":
    st.title("🎁 Combo Deals")
    st.caption("Configure automatic bundle pricing. "
               "Deals auto-apply at checkout.")

    tab1, tab2, tab3 = st.tabs(
        ["Active Deals", "➕ Add Deal", "📈 Deal Performance"])

    with tab1:
        deals = get_deals() if deals_ws else []
        if deals:
            df = pd.DataFrame(deals)
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.caption("💡 Edit or deactivate deals directly in the "
                       "'Combo Deals' tab of your Google Sheet.")
        else:
            st.info("No active deals. Add one in the next tab!")

    with tab2:
        if not deals_ws:
            st.error("Add a 'Combo Deals' tab to your Google Sheet first.")
        else:
            with st.form("add_deal", clear_on_submit=True):
                name = st.text_input("Deal name *",
                    placeholder="e.g. 3 Charms for $20")
                dtype = st.selectbox("Deal type", [
                    "qty_same", "mix_category",
                    "bundle", "bogo", "cart_percent"],
                    help="qty_same = X of same item for $Y • "
                         "mix_category = X of any in category for $Y • "
                         "bundle = specific items for $Y • "
                         "bogo = buy X get cheapest free • "
                         "cart_percent = % off orders over $X")
                c1, c2 = st.columns(2)
                trigger_qty = c1.number_input(
                    "Trigger quantity", min_value=0, step=1)
                combo_price = c2.number_input(
                    "Combo price ($)", min_value=0.0, step=0.50,
                    help="For cart_percent, this is the threshold amount")
                c3, c4 = st.columns(2)
                trigger_cat = c3.text_input(
                    "Category (if applicable)",
                    placeholder="Sticker, Charm, Print...")
                discount_pct = c4.number_input(
                    "Discount % (BOGO/cart_percent)",
                    min_value=0.0, max_value=100.0, step=1.0)
                trigger_items = st.text_input(
                    "Item IDs for bundles (comma-separated)",
                    placeholder="e.g. 5,12")

                if st.form_submit_button("➕ Add Deal", type="primary"):
                    if not name.strip():
                        st.error("Deal name required.")
                    else:
                        new_id = next_id(deals_ws.get_all_records())
                        deals_ws.append_row([
                            new_id, name.strip(), dtype,
                            int(trigger_qty), trigger_cat.strip(),
                            trigger_items.strip(), combo_price,
                            discount_pct, "TRUE"
                        ])
                        clear_cache()
                        st.success(f"Added '{name}'!")

    with tab3:
        if combo_log_ws:
            logs = combo_log_ws.get_all_records()
            if logs:
                df = pd.DataFrame(logs)
                df["discount_amount"] = pd.to_numeric(
                    df["discount_amount"], errors="coerce").fillna(0)

                perf = df.groupby("deal_name").agg(
                    times_used=("id", "count"),
                    total_discount=("discount_amount", "sum"),
                    items_moved=("items_qty", "sum")
                ).sort_values("times_used", ascending=False)

                st.subheader("Which deals are working?")
                st.dataframe(perf.style.format({
                    "total_discount": "${:.2f}"
                }), use_container_width=True)

                st.bar_chart(perf["times_used"])
                st.caption(
                    f"Total discounts given: "
                    f"**${df['discount_amount'].sum():.2f}** "
                    f"across **{len(df)}** applied deals")
            else:
                st.info("No combo deals used yet. "
                        "They'll show up here after your first combo sale!")
        else:
            st.info("Add a 'Combo Sales Log' tab to your Google Sheet "
                    "to enable performance tracking.")

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
        event_filter = st.selectbox("Filter by event",
            ["All events"] + events)
        if event_filter != "All events":
            df = df[df["event"] == event_filter]
        df = df.sort_values("sold_at", ascending=False)
        st.dataframe(df, use_container_width=True, hide_index=True)
        total_shown = pd.to_numeric(df["total"], errors="coerce").sum()
        st.caption(f"Showing {len(df)} sales • "
                   f"Total: **${total_shown:.2f}**")

# ================================================================
# 📊 DASHBOARD
# ================================================================
elif page == "📊 Dashboard":
    st.title("📊 Dashboard")
    sales = get_sales()
    sale_items = get_sale_items()

    if not sales:
        st.info("No sales data yet.")
    else:
        df_sales = pd.DataFrame(sales)
        df_sales["total"] = pd.to_numeric(
            df_sales["total"], errors="coerce").fillna(0)
        df_sales["sold_at"] = pd.to_datetime(
            df_sales["sold_at"], errors="coerce")
        df_sales["event"] = df_sales["event"].fillna(
            "(no event)").replace("", "(no event)")

        today = pd.Timestamp(date.today())
        week_ago = today - pd.Timedelta(days=7)

        rev_today = df_sales[df_sales["sold_at"] >= today]["total"].sum()
        rev_week = df_sales[df_sales["sold_at"] >= week_ago]["total"].sum()
        rev_all = df_sales["total"].sum()

        df_items = pd.DataFrame(sale_items) if sale_items else pd.DataFrame()
        profit = 0
        if not df_items.empty:
            df_items["unit_price"] = pd.to_numeric(
                df_items["unit_price"], errors="coerce").fillna(0)
            df_items["unit_cost"] = pd.to_numeric(
                df_items["unit_cost"], errors="coerce").fillna(0)
            df_items["qty"] = pd.to_numeric(
                df_items["qty"], errors="coerce").fillna(0)
            profit = ((df_items["unit_price"] - df_items["unit_cost"])
                      * df_items["qty"]).sum()

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Today", f"${rev_today:.2f}")
        m2.metric("Last 7 days", f"${rev_week:.2f}")
        m3.metric("All-time", f"${rev_all:.2f}")
        m4.metric("Est. profit", f"${profit:.2f}")

        # ============ REVENUE BY CONVENTION ============
        st.markdown("---")
        st.subheader("🏪 Revenue by Convention")

        by_event = df_sales.groupby("event").agg(
            revenue=("total", "sum"),
            sales_count=("id", "count")
        ).sort_values("revenue", ascending=False)

        if not df_items.empty:
            df_items["profit_line"] = (
                (df_items["unit_price"] - df_items["unit_cost"])
                * df_items["qty"])
            sale_to_event = dict(zip(
                df_sales["id"].astype(str), df_sales["event"]))
            df_items["event"] = (df_items["sale_id"].astype(str)
                                  .map(sale_to_event).fillna("(no event)"))
            event_profit = df_items.groupby("event")["profit_line"].sum()
            by_event["est_profit"] = (by_event.index
                                       .map(event_profit).fillna(0))

        by_event["avg_sale"] = by_event["revenue"] / by_event["sales_count"]

        ec1, ec2 = st.columns([1, 1])
        with ec1:
            st.bar_chart(by_event["revenue"])
        with ec2:
            display_df = by_event.copy()
            display_df["revenue"] = display_df["revenue"].map("${:.2f}".format)
            display_df["avg_sale"] = display_df["avg_sale"].map("${:.2f}".format)
            if "est_profit" in display_df.columns:
                display_df["est_profit"] = display_df["est_profit"].map(
                    "${:.2f}".format)
            st.dataframe(display_df, use_container_width=True)

        # Deep dive
        st.markdown("**🔍 Deep dive into one event:**")
        selected = st.selectbox("Pick an event",
            options=list(by_event.index))
        if selected:
            event_sales = df_sales[df_sales["event"] == selected]
            ec1, ec2, ec3 = st.columns(3)
            ec1.metric("Total revenue",
                       f"${event_sales['total'].sum():.2f}")
            ec2.metric("Number of sales", len(event_sales))
            ec3.metric("Avg sale value",
                       f"${event_sales['total'].mean():.2f}")

            pay_breakdown = event_sales.groupby("payment")["total"].sum()
            st.write("**By payment method:**")
            st.bar_chart(pay_breakdown)

            if not df_items.empty:
                event_items = df_items[df_items["event"] == selected]
                top_items = (event_items.groupby("item_name")["qty"]
                             .sum().sort_values(ascending=False).head(10))
                if not top_items.empty:
                    st.write("**Top items at this event:**")
                    st.bar_chart(top_items)

        # ============ TOP SELLERS OVERALL ============
        st.markdown("---")
        if not df_items.empty:
            st.subheader("🏆 Top Sellers (All Events)")
            top = (df_items.groupby("item_name")["qty"].sum()
                   .sort_values(ascending=False).head(10))
            st.bar_chart(top)

        # ============ DAILY REVENUE ============
        st.subheader("📅 Daily Revenue")
        daily = df_sales.groupby(df_sales["sold_at"].dt.date)["total"].sum()
        st.line_chart(daily)

    # Low stock alert
    inv = get_inventory()
    if inv:
        low = [i for i in inv if int(i.get("stock", 0) or 0) <= 3]
        if low:
            st.subheader("⚠️ Low Stock")
            st.dataframe(pd.DataFrame(low)[["name", "stock"]],
                         hide_index=True)