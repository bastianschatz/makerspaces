"""
Streamlit-App: Makerspaces an Schulen in Bayern
----------------------------------------------
Vollständige, getestete Version (2025-07-01)
• Alle Schularten (OSM)
• MarkerCluster: grün ≥ 1 Makerspace, rot sonst, kein Coverage-Hover
• Multiselect ohne Vorauswahl
• Session-Cache – Karte wird nur neu gebaut, wenn Filter oder DB sich ändern
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
# Konfiguration
###############################################################################
BASE_DIR = Path(__file__).parent
SCHOOL_CACHE = BASE_DIR / "schools_bavaria.csv"
SPACE_FILE = BASE_DIR / "makerspaces.json"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
ADMIN_PASSWORD = os.getenv("MAKERSPACE_ADMIN_PW") or (
    st.secrets.get("makerspace_admin_pw") if hasattr(st, "secrets") else None
) or "changeme"

###############################################################################
# Helper
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
    for typ, pat in patterns.items():
        if re.search(pat, lower):
            return typ
    return "Sonstige"

###############################################################################
# Daten laden
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
    rows = []
    for el in els:
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


def load_db(schools: pd.DataFrame) -> dict[str, dict]:
    if SPACE_FILE.exists():
        raw = json.loads(SPACE_FILE.read_text())
    else:
        raw = {}
    db = {}
    for k, v in raw.items():
        db[k] = v[0] if isinstance(v, list) else (v if isinstance(v, dict) else {})
    for name in schools["name"]:
        db.setdefault(name, {})
    SPACE_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2))
    return db

###############################################################################
# Streamlit UI
###############################################################################

st.set_page_config(page_title="Makerspaces Bayern", layout="wide")

st.title("🛠️ Makerspaces an Schulen in Bayern")

schools_df = load_schools()
db = load_db(schools_df)

with st.sidebar:
    st.header("Filter & Verwaltung")
    sel_types = st.multiselect("Schularten", sorted(schools_df["type"].unique()), default=[])
    filtered_df = schools_df[schools_df["type"].isin(sel_types)] if sel_types else schools_df.iloc[0:0]

    st.divider()
    st.subheader("Makerspace bearbeiten")
    school = st.selectbox("Schule wählen", (filtered_df if not filtered_df.empty else schools_df)["name"].sort_values())
    entry = db.get(school, {})

    space_name = st.text_input("Makerspace-Name", value=entry.get("space_name", ""))
    tools_str = st.text_area("Werkzeuge", value=", ".join(entry.get("tools", [])), height=160)
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
            st.session_state.pop("map_cache_key", None)
            st.success("Gespeichert ✓")
    with col2:
        if entry.get("space_name"):
            pw = st.text_input("Passwort", type="password")
            if st.button("Löschen") and pw == ADMIN_PASSWORD:
                db[school] = {}
                SPACE_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2))
                st.session_state.pop("map_cache_key", None)
                st.success("Gelöscht 🗑️")

###############################################################################
# Karte
###############################################################################

def build_map(df: pd.DataFrame, spaces: dict[str, dict]) -> folium.Map:
    m = folium.Map(location=[48.97, 11.5], zoom_start=7)
    cluster = MarkerCluster(
        options={"showCoverageOnHover": False, "chunkedLoading": True},
        icon_create_function="""
        function(cluster){
            const has = cluster.getAllChildMarkers().some(m=>m.options.hasSpace);
            const color = has ? 'green' : 'red';
            const count = cluster.getChildCount();
            return L.divIcon({html:`<div style='background:${color};border-radius:50%;width:32px;height:32px;display:flex;align-items:center;justify-content:center;color:white;font-weight:bold;'>${count}</div>`});
        }""",
    ).add_to(m)

    for _, r in df.iterrows():
        info = spaces.get(r["name"], {})
        has_space = bool(info.get("space_name"))
        color = "green" if has_space else "red"

        popup = [f"<b>{r['name']}</b>", f"<br><i>{r['type']}</i>"]
        if has_space:
            if info.get("contact"):
                popup.append(f"<br><b>Kontakt:</b> {info['contact']}")
            if info.get("email"):
                popup.append(f"<br><b>Email:</b> <a href='mailto:{info['email']}'>{info['email']}</a>")
            if info.get("website"):
                popup.append(f"<br><b>Web:</b> <a href='{info['website']}' target='_blank'>{info['website']}</a>")
            tools = ", ".join(info.get("tools", [])) or "–"
            popup.append(f"<hr style='margin:4px 0;'><i>{info['space_name']}</i><br>Werkzeuge: {tools}")
        else:
            popup.append("<br><i>Kein Makerspace eingetragen.</i>")

        marker = folium.CircleMarker(
            location=[r["lat"], r["lon"]], radius=6,
            color=color, fill=True, fillColor=color, fillOpacity=0.9,
        )
        marker.options["hasSpace"] = has_space
        marker.add_child(folium.Popup("".join(popup), max_width=300))
        marker.add_to(cluster)

    Fullscreen().add_to(m)
    LocateControl().add