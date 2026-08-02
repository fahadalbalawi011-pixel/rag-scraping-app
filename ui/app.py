"""
Streamlit UI for browsing scraped listings.

Mostly a read-only view over the SQLite database: the sidebar builds filters,
which are passed straight to db.database.query_listings (the UI never writes
SQL itself). The two things it DOES write are your own annotations, not
scraped data — liking a listing (favorites) and typing a category onto one
(custom tags).

Run from the project root:

    streamlit run ui/app.py
"""

import sys
from pathlib import Path

# `streamlit run ui/app.py` puts ui/ on the import path, not the project root,
# so the db/config packages aren't importable by default. Add the project
# root (one level up from this file) so those imports resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import math

import pandas as pd
import streamlit as st

from db.database import (
    add_tag,
    count_favorites,
    get_all_tags,
    get_distinct_values,
    get_favorite_ids,
    get_numeric_percentile,
    get_numeric_range,
    get_tags_for_listings,
    init_db,
    query_listings,
    remove_tag,
    toggle_favorite,
)

# Make sure the DB/tables exist even if the app is opened before any scrape.
# This also creates the `favorites` table on an existing database.
init_db()

st.set_page_config(page_title="عقارات جدة", page_icon="🏠", layout="wide")

# Streamlit is left-to-right by default; flip the whole app to right-to-left
# so Arabic text and layout read naturally. Note this also reverses the
# horizontal axis of st.columns (they're flexbox rows), which is why the
# FIRST column of each card's control row is the one that appears on the
# right — that's where the favorite button goes.
st.markdown(
    """
    <style>
      .stApp, .stApp * { direction: rtl; text-align: right; }

      /* --- Listing card text ------------------------------------------
         Each line sets its own bottom margin. There is deliberately no
         `.listing-card > div { margin: 0 }`-style rule here: a selector like
         that (one class + one element = specificity 0,1,1) outranks the
         single-class `.lc-price` rules (0,1,0) and silently flattened every
         margin below, which is what made the cards look cramped. */
      .lc-title { font-size: 1.05rem; font-weight: 600; line-height: 1.6;
                  margin: 0 0 0.55rem 0; }
      .lc-price { font-size: 1.2rem; font-weight: 700; color: #16a34a;
                  margin: 0 0 0.15rem 0; }
      .lc-ppsm  { font-size: 0.85rem; opacity: 0.6; margin: 0 0 0.55rem 0; }
      .lc-facts { opacity: 0.85; margin: 0 0 0.4rem 0; }
      .lc-loc   { opacity: 0.7; font-size: 0.9rem; margin: 0 0 0.2rem 0; }

      /* Category chips (your own labels) and the "off-market" badge. */
      .lc-chips { margin: 0.6rem 0 0 0; }
      .lc-chip  { display: inline-block; margin: 0 0 4px 5px; padding: 1px 10px;
                  border-radius: 999px; font-size: 0.8rem;
                  background: rgba(22,163,74,0.12); color: #16a34a;
                  border: 1px solid rgba(22,163,74,0.30); }
      .lc-badge { display: inline-block; padding: 1px 9px; border-radius: 6px;
                  font-size: 0.8rem; background: rgba(220,38,38,0.12);
                  color: #dc2626; border: 1px solid rgba(220,38,38,0.30); }
      .lc-link a { text-decoration: none; font-weight: 600; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🏠 عقارات جدة")

# Arabic display name per source site, so each listing's link names the site
# it actually came from (not always "عقار"). Falls back to the raw site id.
SITE_NAMES = {
    "aqar.fm": "عقار",
    "wasalt.sa": "وصلت",
    "bayut.sa": "بيوت",
    "dealapp.sa": "ديل اب",
}

# Slider bounds are taken at this percentile instead of the true MAX. A few
# listings carry corrupt prices (the worst: 130 billion riyals for a 600 m²
# plot), and letting them set the scale made both sliders useless. Maxing a
# slider out still means "no upper limit", so those listings stay reachable.
BOUND_PERCENTILE = 99

PAGE_SIZE = 30


# Cache the filter-option/range lookups so they hit the DB once instead of on
# every rerun (every time you touch a widget the whole script re-runs). The
# cache is cleared with the "تحديث البيانات" button after a new scrape.
@st.cache_data
def _cached_distinct(column):
    return get_distinct_values(column)


@st.cache_data
def _cached_range(column):
    return get_numeric_range(column)


@st.cache_data
def _cached_percentile(column, percentile):
    return get_numeric_percentile(column, percentile)


def _bounds(column, step):
    """
    Slider (min, max, step) for a numeric column, rounded outward to tidy
    step values so the extremes are never cut off by a handle.
    """
    low, _true_high = _cached_range(column)
    high = _cached_percentile(column, BOUND_PERCENTILE)
    lo = int(math.floor(low / step) * step)
    hi = int(math.ceil(high / step) * step)
    if hi <= lo:  # no data yet (or all values equal): give the slider room
        hi = lo + step
    return lo, hi, step


def _fmt_price(price):
    """
    Full riyal amount with thousands separators, plus a compact reading for
    large numbers — Saudi listings are usually discussed in millions, so
    "1,350,000 ريال (1.35 مليون)" is faster to scan than digits alone.
    """
    if price is None or pd.isna(price):
        return "السعر غير محدد"
    text = f"{price:,.0f} ريال"
    if price >= 1_000_000:
        millions = price / 1_000_000
        # Trim a trailing ".0" so 2 million reads "2 مليون", not "2.0 مليون".
        compact = f"{millions:.2f}".rstrip("0").rstrip(".")
        text += f" ({compact} مليون)"
    return text


def _add_tag_from_input(listing_id: int, widget_key: str):
    """
    on_change handler for a card's category box: save what was typed, then
    clear the box so the next category can be typed straight away.

    Runs before the rerun that follows the keystroke, so the new chip is
    already in the database by the time the card re-renders.
    """
    typed = (st.session_state.get(widget_key) or "").strip()
    if typed:
        add_tag(listing_id, typed)
    st.session_state[widget_key] = ""


# --- Sidebar filters ------------------------------------------------------
# Options come from the DB so the dropdowns only ever show values that
# actually exist in the current data.
st.sidebar.header("تصفية النتائج")

if st.sidebar.button("🔄 تحديث البيانات"):
    st.cache_data.clear()

favorites_total = count_favorites()
# The explicit key is load-bearing, not decoration. Streamlit derives a
# keyless widget's identity from its parameters — including its label — so
# because this label contains the favorites count, liking or unliking
# something changed the label, which made Streamlit treat it as a NEW widget
# and silently reset it to value=False. The visible symptom was that
# unliking a listing while in the favorites view kicked you back to the full
# 39k list. A stable key pins the identity so the state survives the relabel.
show_favorites = st.sidebar.toggle(
    f"❤️ المفضلة فقط ({favorites_total})",
    value=False,
    key="show_favorites_only",
    help="اعرض العقارات التي أضفتها إلى المفضلة فقط.",
)

property_types = _cached_distinct("property_type")
districts = _cached_distinct("district")

selected_type = st.sidebar.selectbox(
    "نوع العقار", options=["الكل"] + property_types, index=0
)
selected_district = st.sidebar.selectbox(
    "الحي", options=["الكل"] + districts, index=0
)

# Category options aren't cached: they change as soon as you type a new one
# onto a card, and the list is small enough that a fresh read is free.
all_tags = get_all_tags()
selected_tags = st.sidebar.multiselect(
    "التصنيف",
    options=all_tags,
    default=[],
    placeholder="اختر تصنيفاً",   # else Streamlit shows English "Choose options"
    help="تصنيفاتك الخاصة التي كتبتها على العقارات.",
)

search = st.sidebar.text_input("بحث نصي (العنوان/الوصف)")

price_lo, price_hi, price_step = _bounds("price", 50_000)
area_lo, area_hi, area_step = _bounds("area_sqm", 10)

price_min, price_max = st.sidebar.slider(
    "السعر (ريال)",
    min_value=price_lo,
    max_value=price_hi,
    value=(price_lo, price_hi),
    step=price_step,
)
area_min, area_max = st.sidebar.slider(
    "المساحة (م²)",
    min_value=area_lo,
    max_value=area_hi,
    value=(area_lo, area_hi),
    step=area_step,
)
# Default ON: thousands of dealapp listings have no area at all (its
# whole-city endpoint doesn't report one), and excluding them by default made
# them disappear the moment the slider was touched, with nothing to explain it.
include_unknown_area = st.sidebar.checkbox(
    "أضف العقارات بمساحة غير معروفة",
    value=True,
    help="بعض الإعلانات لا تذكر المساحة. أزل هذا الخيار لإظهار العقارات ذات "
         "المساحة المعروفة فقط.",
)
st.sidebar.caption(
    f"الحد الأعلى للمنزلقات محسوب على أعلى {BOUND_PERCENTILE}% من البيانات. "
    "اترك المنزلق على أقصى اليمين لإلغاء الحد الأعلى."
)


# --- Run the query --------------------------------------------------------
# "الكل" (= "All") means "don't filter on this field", so we pass None.
# A slider left at its ceiling also means None — that's what keeps the
# out-of-scale listings reachable even though they don't set the bounds.
#
# active_only is relaxed in the favorites view: a listing you liked is kept
# in the database after it leaves the source site (purge_old_inactive skips
# favorites), so hiding inactive rows here would make your own list lose
# entries. Those rows are shown with an "off-market" badge instead.
df = query_listings(
    search=search or None,
    property_type=None if selected_type == "الكل" else selected_type,
    district=None if selected_district == "الكل" else selected_district,
    price_min=price_min if price_min > price_lo else None,
    price_max=price_max if price_max < price_hi else None,
    area_min=area_min if area_min > area_lo else None,
    area_max=area_max if area_max < area_hi else None,
    include_unknown_area=include_unknown_area,
    tags=selected_tags or None,
    favorites_only=show_favorites,
    active_only=not show_favorites,
)

st.caption(f"عدد النتائج: {len(df)}")


# --- Results (paginated) --------------------------------------------------
# Rendering every card is what made the UI slow: with thousands of matches,
# Streamlit rebuilds thousands of containers on each rerun. So we show one
# page at a time.
if df.empty:
    if show_favorites and not selected_tags:
        st.info("لم تضف أي عقار إلى المفضلة بعد. اضغط على 🤍 في أي عقار لإضافته.")
    else:
        st.info("لا توجد نتائج مطابقة للتصفية الحالية.")
else:
    total_pages = (len(df) - 1) // PAGE_SIZE + 1
    page = 1
    if total_pages > 1:
        page = st.number_input(
            f"الصفحة (من {total_pages})",
            min_value=1,
            max_value=total_pages,
            value=1,
            step=1,
        )
    start = (page - 1) * PAGE_SIZE
    page_df = df.iloc[start : start + PAGE_SIZE]

    # Two queries for the whole page instead of two per card: which listings
    # are liked, and what categories each one carries.
    favorite_ids = get_favorite_ids()
    tags_by_listing = get_tags_for_listings(page_df["id"].tolist())

    for _, row in page_df.iterrows():
        listing_id = int(row["id"])
        title = row["title"] or "بدون عنوان"

        facts = [f"النوع: {row['property_type'] or '-'}"]
        if pd.notna(row["area_sqm"]):
            facts.append(f"المساحة: {int(row['area_sqm'])} م²")
        if pd.notna(row["bedrooms"]):
            facts.append(f"غرف: {int(row['bedrooms'])}")
        if pd.notna(row["bathrooms"]):
            facts.append(f"دورات مياه: {int(row['bathrooms'])}")
        facts_line = " • ".join(facts)

        location = "، ".join(
            p for p in [row["district"], row["city"]] if pd.notna(p) and p
        )
        site = SITE_NAMES.get(row["source_site"], row["source_site"])
        link = (
            f'<a href="{row["source_url"]}" target="_blank">عرض الإعلان على {site} ↗</a>'
            if row["source_url"]
            else ""
        )

        # price_per_sqm is computed at insert time but was never surfaced;
        # it's the most useful number for comparing two listings.
        ppsm = ""
        if pd.notna(row["price_per_sqm"]):
            ppsm = f'<div class="lc-ppsm">{row["price_per_sqm"]:,.0f} ريال / م²</div>'

        listing_tags = tags_by_listing.get(listing_id, [])
        chips = ""
        if listing_tags:
            chips = '<div class="lc-chips">' + "".join(
                f'<span class="lc-chip">{t}</span>' for t in listing_tags
            ) + "</div>"

        badge = ""
        if not row["is_active"]:
            badge = '<span class="lc-badge">لم يعد متاحاً على الموقع</span>'

        with st.container(border=True):
            st.markdown(
                f"""
                <div class="lc-title">{title}</div>
                <div class="lc-price">{_fmt_price(row["price"])}</div>
                {ppsm}
                <div class="lc-facts">{facts_line}</div>
                {f'<div class="lc-loc">📍 {location}</div>' if location else ''}
                {badge}
                {chips}
                """,
                unsafe_allow_html=True,
            )

            # Control row. Under RTL the first column renders on the RIGHT,
            # so the favorite button sits on the right below the price.
            c_fav, c_tag, c_link = st.columns([1, 3, 4], vertical_alignment="center")

            liked = listing_id in favorite_ids
            with c_fav:
                # on_click (not the button's return value) so the toggle is
                # applied BEFORE the rerun re-reads favorite_ids — otherwise
                # the heart would show its old state for one interaction.
                st.button(
                    "❤️" if liked else "🤍",
                    key=f"fav_{listing_id}",
                    on_click=toggle_favorite,
                    args=(listing_id,),
                    help="إزالة من المفضلة" if liked else "إضافة إلى المفضلة",
                )

            with c_tag:
                tag_key = f"tag_input_{listing_id}"
                st.text_input(
                    "تصنيف",
                    key=tag_key,
                    placeholder="اكتب تصنيفاً ثم Enter…",
                    label_visibility="collapsed",
                    on_change=_add_tag_from_input,
                    args=(listing_id, tag_key),
                )

            with c_link:
                if listing_tags:
                    with st.popover("🏷️ التصنيفات"):
                        st.caption("اضغط لإزالة تصنيف:")
                        for tag in listing_tags:
                            st.button(
                                f"✕ {tag}",
                                key=f"untag_{listing_id}_{tag}",
                                on_click=remove_tag,
                                args=(listing_id, tag),
                            )
                if link:
                    st.markdown(f'<div class="lc-link">{link}</div>',
                                unsafe_allow_html=True)
