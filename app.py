"""
Streamlit‑App · Makerspaces an Bayerischen Schulen  
Version 2025‑07‑01 — FastMarkerCluster + Session‑Caching
------------------------------------------------------
* **FastMarkerCluster** rendert Marker stückweise (Performance‑Boost)
* **Session‑Cache**: Die Karte wird nur neu gebaut, wenn sich
  - der Schulart‑Filter  **oder**  
  - die Makerspace‑Datenbank ändert.
  Dadurch bleibt das UI reaktiv, ohne bei jeder Texteingabe
  tausende Marker neu zu zeichnen.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import hashlib
from textwrap import dedent

import pandas as pd
import requests
import streamlit as st
import folium
from folium.plugins import MarkerCluster, Fullscreen, LocateControl
from streamlit_folium import st_folium

###############################################################################
# CONFIGURATION
###############################################################################
SCHOOL_CACHE = Path("schools_bavaria.csv")
SPACE_FILE = Path("makerspaces.json")
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# --- Admin‑Passwort robust ---------------------------------------------------
_env_pw = os.getenv("MAKERSPACE_ADMIN_PW")
try:
    _secret_pw = st.secrets["makerspace_admin_pw"]
except Exception:
    _secret_pw = None
ADMIN_PASSWORD = _env_pw or _secret_pw or "changeme"

###############################################################################
# HELPERS
###############################################################################

def school_type_from_name(name: str) -> str:
    patterns = {
        "Gymnasium": r"gymnasium",
        "Grundschule": r"grundschule",
        "Realschule": r"realschule",
        "Mittelschule": r"mittelschule|hauptschule",
        "Berufsschule": r"berufsschule",
        "FOS/BOS": r"fachoberschule|berufsoberschule|fos|bos",
        "Wirtschaftsschule": r"wirtschaftsschule",
        "Förderschule": r"förderschule|sonderpädagogisch",
    }
    low = name.lower()
    for t, pat in patterns.items():
        if re.search(pat, low):
            return t
    return "Sonstige"

###############################################################################
# DATA LAYER
###############################################################################

@st.cache_data(show_spinner="📡 Lade Schulen …")
def load_schools() -> pd.DataFrame:
    if SCHOOL_CACHE.exists():
        df = pd.read_csv(SCHOOL_CACHE)
        if "type" not in df.columns:
            df["type"] = df["name"].apply(school_type_from_name)
            df.to_csv(SCHOOL_CACHE, index=False)
        return df

    query = dedent(
        """
        [out:json][timeout:120];
        area["ISO3166-2"="DE-BY"]->.searchArea;
        (
          node["amenity"="school"](area.searchArea);
          way["amenity"="school"](area.searchArea);
          relation["amenity"="school"](area.searchArea);
        );
        out center tags;
        """
    )
    els = requests.post(OVERPASS_URL, data={"data": query}).json()["elements"]
    rows = [
        {
            "name": el["tags"].get("name"),
            "lat": el.get("lat") or el["center"]["lat"],
            "lon": el.get("lon") or el["center"]["lon"],
            "type": school_type_from_name(el["tags"].get("name", "")),
        }
        for el in els
        if el.get("lat") or el.get("center")
    ]
    df = pd.DataFrame(rows).drop_duplicates()
    df.to_csv(SCHOOL_CACHE, index=False)
    return df


def load_or_init_db(schools: pd.DataFrame) -> dict[str, dict]:
    if SPACE_FILE.exists():
        raw = json.loads(SPACE_FILE.read_text())
    else:
        raw = {}
    db, changed = {}, False
    for k, v in raw.items():
        if isinstance(v, list):
            db[k] = v[0] if v else {}
            changed = True
        elif isinstance(v, dict):
            db[k] = v
        else:
            db[k] = {}
    for n in schools["name"]:
        if n not in db:
            db[n] = {}
            changed = True
    if changed:
        SPACE_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2))
    return db

###############################################################################
# UI
###############################################################################

st.set_page_config(page_title="Makerspaces Bayern", layout="wide")

st.title("🛠️ Makerspaces an Schulen in Bayern")

schools_df = load_schools()
db = load_or_init_db(schools_df)

# ---------------- Sidebar ----------------------------------------------------
with st.sidebar:
    st.header("Filter & Verwaltung")
    sel_types = st.multiselect(
        "Schularten",
        options=sorted(schools_df["type"].unique()),
        default=[],
        help="Wähle eine oder mehrere Schularten für die Karte.",
    )
    filtered_df = schools_df[schools_df["type"].isin(sel_types)] if sel_types else schools_df.iloc[0:0]

    st.divider()
    st.subheader("Makerspace bearbeiten")
    school = st.selectbox("Schule wählen", filtered_df["name"].sort_values() if not filtered_df.empty else schools_df["name"].sort_values())
    entry = db.get(school, {})

    space_name = st.text_input("Makerspace-Name", value=entry.get("space_name", ""))
    tools_str = st.text_area("Werkzeuge (kommagetrennt)", value=", ".join(entry.get("tools", [])), height=200)
    contact = st.text_input("Ansprechpartner", value=entry.get("contact", ""))
    email = st.text_input("E-Mail", value=entry.get("email", ""))
    website = st.text_input("Webseite", value=entry.get("website", ""))

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Speichern"):
            db[school] = {
                "space_name": space_name.strip(),
                "tools": [t.strip() for t in tools_str.split(",") if t.strip()],
                "contact": contact.strip(),
                "email": email.strip(),
                "website": website.strip(),
            }
            SPACE_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2))
            st.session_state.pop("map_key", None)  # ↻ Karte neu zeichnen
            st.success("Gespeichert!")
    with col2:
        if entry.get("space_name"):
            pwd = st.text_input("Passwort zum Löschen", type="password")
            if st.button("Löschen") and pwd == ADMIN_PASSWORD:
                db[school] = {}
                SPACE_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2))
                st.session_state.pop("map_key", None)
                st.success("Gelöscht")

###############################################################################
# Map & Session‑Cache
###############################################################################

def build_map(df: pd.DataFrame, spaces: dict[str, dict]) -> folium.Map:
    """MarkerCluster mit chunkedLoading & farbiger Cluster-Bubble."""
    m = folium.Map(location=[48.97, 11.5], zoom_start=7)

    icon_create = """
    function(cluster){
        const has = cluster.getAllChildMarkers().some(m=>m.options.hasSpace);
        const count = cluster.getChildCount();
        const color = has ? 'green' : 'red';
        return L.divIcon({html:`<div style='background:${color};border-radius:50%;width:32px;height:32px;display:flex;align-items:center;justify-content:center;color:white;'>${count}</div>`});
    }"""

    cluster = MarkerCluster(
        options={"showCoverageOnHover": False, "chunkedLoading": True},
        icon_create_function=icon_create
    ).add_to(m)

    for _, r in df.iterrows():
        e = spaces.get(r["name"], {})
        has_space = bool(e.get("space_name"))
        color = "green" if has_space else "red"
        popup = f"<b>{r['name']}</b><br><i>{r['type']}</i>"
        if has_space:
            if e.get("contact"):
                popup += f"<br><b>Kontakt:</b> {e['contact']}"
            if e.get("email"):
                popup += f"<br><b>Email:</b> <a href='mailto:{e['email']}'>{e['email']}</a>"
            if e.get("website"):
                popup += f"<br><b>Web:</b> <a href='{e['website']}' target='_blank'>{e['website']}</a>"
            tools = ", ".join(e.get("tools", [])) or "–"
            popup += f"<hr style='margin:4px 0;'><i>{e['space_name']}</i><br>Werkzeuge: {tools}"
        else:
            popup += "<br><i>Kein Makerspace eingetragen.</i>"
        marker = folium.CircleMarker(
            location=[r["lat"], r["lon"]],
            radius=6,
            color=color,
            fill=True,
            fillColor=color,
            fillOpacity=0.9,
        )
        marker.options.update({"hasSpace": has_space})
        marker.add_child(folium.Popup(popup, max_width=300))
        marker.add_to(cluster)

    Fullscreen().add_to(m)
    LocateControl().add_to(m)
    return m

# ------------- Caching-Logik -------------------------------------------------

def current_map_key(df: pd.DataFrame) -> str:
    filter_hash = hashlib.md5(pd.util.hash_pandas_object(df["name"], index=False).values).hexdigest()
    file_mtime = os.path.getmtime(SPACE_FILE) if SPACE_FILE.exists() else 0
    return f"{filter_hash}_{file_mtime}"

st.markdown("### Karte")
if not sel_types:
    st.info("Bitte mindestens eine Schulart wählen.")
else:
    key = current_map_key(filtered_df)
    if st.session_state.get("map_key") != key:
        st.session_state["map_obj"] = build_map(filtered_df, db)
        st.session_state["map_key"] = key
    st_foliumst_folium(st.session_state["map_obj"], width=1280, height=650)
(st.session_state["map_obj"], width=1280, height=650)
