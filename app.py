"""
Streamlit‑App: Makerspaces an Schulen in Bayern
==============================================

* Alle Schularten (OpenStreetMap)
* MarkerCluster mit farbiger Bubble (grün ≥1 Makerspace, sonst rot)
* Keine Schulart vorgewählt → User wählt aktiv
* Session‑Cache: Karte wird nur neu gerechnet, wenn Filter oder Datenbank sich ändern
* Kein blauer Coverage‑Overlay (showCoverageOnHover=False)
* Fastes Rendering dank `chunkedLoading: true` + CircleMarker
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from textwrap import dedent

import pandas as pd
import requests
import streamlit as st
import folium
from folium.plugins import MarkerCluster, Fullscreen, LocateControl
from streamlit_folium import st_folium

###############################################################################
# KONFIGURATION
###############################################################################
BASE_DIR = Path(__file__).parent
SCHOOL_CACHE = BASE_DIR / "schools_bavaria.csv"
SPACE_FILE = BASE_DIR / "makerspaces.json"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Admin‑Passwort (Env > secrets.toml > Fallback)
_env_pw = os.getenv("MAKERSPACE_ADMIN_PW")
try:
    _secret_pw = st.secrets["makerspace_admin_pw"]  # nur falls vorhanden
except Exception:
    _secret_pw = None
ADMIN_PASSWORD = _env_pw or _secret_pw or "changeme"

###############################################################################
# HILFSFUNKTIONEN
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
    lower = name.lower()
    for t, pat in patterns.items():
        if re.search(pat, lower):
            return t
    return "Sonstige"

###############################################################################
# DATENEBENE
###############################################################################

@st.cache_data(show_spinner="📡 Lade Schulen aus OpenStreetMap …")
def load_schools() -> pd.DataFrame:
    """CSV‑Cache laden oder via Overpass neu ziehen."""
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
    resp = requests.post(OVERPASS_URL, data={"data": query})
    elems = resp.json()["elements"]
    rows: list[dict] = []
    for el in elems:
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        name = el.get("tags", {}).get("name")
        if lat and lon and name:
            rows.append({
                "name": name,
                "lat": lat,
                "lon": lon,
                "type": school_type_from_name(name),
            })
    df = pd.DataFrame(rows).drop_duplicates()
    df.to_csv(SCHOOL_CACHE, index=False)
    return df


def load_or_init_db(schools: pd.DataFrame) -> dict[str, dict]:
    """Stellt sicher, dass jede Schule einen Key in makerspaces.json hat."""
    if SPACE_FILE.exists():
        raw = json.loads(SPACE_FILE.read_text())
    else:
        raw = {}
    db: dict[str, dict] = {}
    changed = False
    for k, v in raw.items():
        if isinstance(v, list):
            db[k] = v[0] if v else {}
            changed = True
        elif isinstance(v, dict):
            db[k] = v
        else:
            db[k] = {}
    for name in schools["name"]:
        if name not in db:
            db[name] = {}
            changed = True
    if changed:
        SPACE_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2))
    return db

###############################################################################
# UI KONFIGURATION
###############################################################################

st.set_page_config(page_title="Makerspaces Bayern", layout="wide")

st.title("🛠️ Makerspaces an Schulen in Bayern")

schools_df = load_schools()
db = load_or_init_db(schools_df)

# ---------------- Sidebar ----------------------------------------------------
with st.sidebar:
    st.header("Filter & Verwaltung")

    # Schulart‑Filter (keine Vorauswahl)
    sel_types = st.multiselect(
        "Schularten",
        options=sorted(schools_df["type"].unique()),
        default=[],
        help="Wähle eine oder mehrere Schularten für die Karte.",
    )
    if sel_types:
        filtered_df = schools_df[schools_df["type"].isin(sel_types)]
    else:
        filtered_df = schools_df.iloc[0:0]

    st.divider()

    # Makerspace‑Formular
    st.subheader("Makerspace bearbeiten")
    school = st.selectbox(
        "Schule wählen",
        (filtered_df if not filtered_df.empty else schools_df)["name"].sort_values(),
    )
    entry = db.get(school, {})

    space_name = st.text_input("Makerspace‑Name", value=entry.get("space_name", ""))
    tools_str = st.text_area(
        "Werkzeuge (kommagetrennt)",
        value=", ".join(entry.get("tools", [])),
        height=200,
    )
    contact = st.text_input("Ansprechpartner", value=entry.get("contact", ""))
    email = st.text_input("E‑Mail", value=entry.get("email", ""))
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
            st.session_state.pop("map_key", None)  # Karte neu bauen
            st.success("Gespeichert ✓")
    with col2:
        if entry.get("space_name"):
            pwd = st.text_input("Passwort zum Löschen", type="password")
            if st.button("Löschen") and pwd == ADMIN_PASSWORD:
                db[school] = {}
                SPACE_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2))
                st.session_state.pop("map_key", None)
                st.success("Gelöscht 🗑️")

###############################################################################
# MAP‑Builder
###############################################################################

def build_map(df: pd.DataFrame, spaces: dict[str, dict]) -> folium.Map:
    m = folium.Map(location=[48.97, 11.5], zoom_start=7)

    cluster = MarkerCluster(
        options={"showCoverageOnHover": False, "chunkedLoading": True},
        icon_create_function="""
        function(cluster){
            const has = cluster.getAllChildMarkers().some(m=>m.options.hasSpace);
            const count = cluster.getChildCount();
            const color = has ? 'green' : 'red';
            return L.divIcon({html:`<div style='background:${color};border-radius:50%;width:32px;height:32px;display:flex;align-items:center;justify-content:center;color:white;'>${count}</div>`});
        }
        """,
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
                popup += f"<br><b>Email:</
