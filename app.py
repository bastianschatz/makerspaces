"""
Streamlit-App: Makerspaces an Schulen in Bayern — stabile Komplettfassung
========================================================================
* lädt **alle Schularten** aus OpenStreetMap (Overpass) und cacht sie in `schools_bavaria.csv`
* Makerspaces werden in `makerspaces.json` gepflegt, Lösch-Passwort über Umgebungs­variable `MAKERSPACE_ADMIN_PW` oder `st.secrets`
* **MarkerCluster** mit grüner Bubble, falls ≥ 1 Makerspace im Cluster, sonst rot
* **Session-Cache** – Karte wird nur neu berechnet, wenn Schulart-Filter oder Datenbank sich ändern
* **Kein** blauer Coverage-Hover
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

# Admin-Passwort: Env > secrets.toml > Fallback
ADMIN_PASSWORD = os.getenv("MAKERSPACE_ADMIN_PW") or (
    st.secrets.get("makerspace_admin_pw") if hasattr(st, "secrets") else None
) or "changeme"

###############################################################################
# Hilfsfunktionen
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

@st.cache_data(show_spinner="📡 Lade Schulen aus OpenStreetMap …")
def load_schools() -> pd.DataFrame:
    """CSV-Cache laden oder via Overpass neu erzeugen."""
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
    elements = requests.post(OVERPASS_URL, data={"data": query}).json()["elements"]
    rows: list[dict] = []
    for el in elements:
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
    """Makerspace-Datenbank laden, migrieren, fehlende Keys ergänzen."""
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
# Streamlit-Seite
###############################################################################

st.set_page_config(page_title="Makerspaces Bayern", layout="wide")

st.title("🛠️ Makerspaces an Schulen in Bayern")

schools_df = load_schools()
db = load_or_init_db(schools_df)

# ---- Sidebar ---------------------------------------------------------------
with st.sidebar:
    st.header("Filter & Verwaltung")
    # 1) Schulart-Filter (keine Vorauswahl)
    sel_types = st.multiselect(
        "Schularten",
        sorted(schools_df["type"].unique()),
        default=[],
        help="Wähle eine oder mehrere Schularten für die Karte.",
    )
    filtered_df = schools_df[schools_df["type"].isin(sel_types)] if sel_types else schools_df.iloc[0:0]

    st.divider()
    # 2) Makerspace-Formular
    st.subheader("Makerspace bearbeiten")
    school = st.selectbox(
        "Schule wählen",
        (filtered_df if not filtered_df.empty else schools_df)["name"].sort_values(),
    )
    entry = db.get(school, {})

    space_name = st.text_input("Makerspace-Name", value=entry.get("space_name", ""))
    tools_str = st.text_area("Werkzeuge (kommagetrennt)", value=", ".join(entry.get("tools", [])), height=180)
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
            st.session_state.pop("map_key", None)
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
# Karte erzeugen
###############################################################################

def build_map(df: pd.DataFrame, spaces: dict[str, dict]) -> folium.Map:
    m = folium.Map(location=[48.97, 11.5], zoom_start=7)

    cluster = MarkerCluster(
        options={"showCoverageOnHover": False, "chunkedLoading": True},
        icon_create_function="""
        function(cluster){
            const hasSpace = cluster.getAllChildMarkers().some(m=>m.options.hasSpace);
            const color = hasSpace ? 'green' : 'red';
            const count = cluster.getChildCount();
            return L.divIcon({html:`<div style='background:${color};border-radius:50%;width:32px;height:32px;display:flex;align-items:center;justify-content:center;color:white;font-weight:bold;'>${count}</div>`});
        }""",
    ).add_to(m)

    for _, row in df.iterrows():
        info = spaces.get(row["name"], {})
        has_space = bool(info.get("space_name"))
        color = "green" if has_space else "red"

        popup_parts = [f"<b>{row['name']}</b>", f"<br><i>{row['type']}</i>"]
        if has_space:
            if info.get("contact"):
                popup_parts.append(f"<br><b>Kontakt:</b> {info['contact']}")
            if info.get("email"):
                popup_parts.append(f"<br><b>Email:</b> <a href='mailto:{info['email']}'>{info['email']}</a>")
            if info.get("website"):
                popup_parts.append(f"<br><b>Web:</b> <a href='{info['website']}' target='_blank'>{info['website']}</a>")