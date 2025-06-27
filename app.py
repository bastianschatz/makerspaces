"""
Streamlit‑App: Makerspaces an Bayerischen Schulen – Resilience Fix (2025‑06‑29)
-----------------------------------------------------------------------------
* **Fehler behoben:** Wenn bereits eine ältere `schools_bavaria.csv` ohne Spalte `type` existierte, schlug der Zugriff (`KeyError: 'type'`) fehl.  
  → `load_schools()` prüft nun den Cache und ergänzt die Spalte bei Bedarf automatisch.
* Keine weiteren Veränderungen am Verhalten.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
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
ADMIN_PASSWORD = (
    st.secrets.get("makerspace_admin_pw", None)
    or os.getenv("MAKERSPACE_ADMIN_PW", "changeme")
)

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
# DATA
###############################################################################

@st.cache_data(show_spinner="📡 Lade Schulen …")
def load_schools() -> pd.DataFrame:
    """Lädt Schulen aus Cache oder Overpass und sorgt dafür, dass die Spalte 'type' immer vorhanden ist."""
    if SCHOOL_CACHE.exists():
        df = pd.read_csv(SCHOOL_CACHE)
        # ► Cache von alten Versionen ohne 'type' reparieren
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
    rows = []
    for el in elements:
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        name = el.get("tags", {}).get("name")
        if lat and lon and name:
            rows.append({"name": name, "lat": lat, "lon": lon, "type": school_type_from_name(name)})
    df = pd.DataFrame(rows).drop_duplicates()
    df.to_csv(SCHOOL_CACHE, index=False)
    return df


def load_or_init_db(schools: pd.DataFrame) -> dict[str, dict]:
    """Lädt makerspaces.json.
    * Alte Listenstruktur (\[{...}] ) wird in das neue Dict-Format umgewandelt (1. Element).
    * Falls Datei fehlt, Skeleton mit leeren Dicts.
    """
    if SPACE_FILE.exists():
        raw = json.loads(SPACE_FILE.read_text())
        migrated: dict[str, dict] = {}
        for k, v in raw.items():
            if isinstance(v, list):  # altes Schema → nimm ersten Eintrag oder leeres Dict
                migrated[k] = v[0] if v else {}
            elif isinstance(v, dict):
                migrated[k] = v
            else:
                migrated[k] = {}
        # Speichern, falls Migration stattfand
        if migrated != raw:
            SPACE_FILE.write_text(json.dumps(migrated, ensure_ascii=False, indent=2))
        return migrated
    # Datei fehlt: Skeleton
    db = {row["name"]: {} for _, row in schools.iterrows()}
    SPACE_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2))
    return db

###############################################################################
# UI / MAP (unverändert gegenüber Vorversion)
###############################################################################

st.set_page_config(page_title="Makerspaces Bayern", layout="wide")

st.title("🛠️ Makerspaces an Schulen in Bayern")
schools_df = load_schools()
db = load_or_init_db(schools_df)

with st.sidebar:
    st.header("Filter & Verwaltung")
    sel_types = st.multiselect("Schularten", options=sorted(schools_df["type"].unique()),
                               default=sorted(schools_df["type"].unique()))
    filtered_df = schools_df[schools_df["type"].isin(sel_types)]

    st.divider()
    st.subheader("Makerspace bearbeiten")
    school = st.selectbox("Schule wählen", filtered_df["name"].sort_values())
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
            st.success("Gespeichert!")
    with col2:
        if entry and entry.get("space_name"):
            pwd = st.text_input("Passwort zum Löschen", type="password")
            if st.button("Löschen") and pwd == ADMIN_PASSWORD:
                db[school] = {}
                SPACE_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2))
                st.success("Gelöscht")

# ----- Karte -----------------------------------------------------------------

def build_map(df: pd.DataFrame, spaces: dict[str, dict]) -> folium.Map:
    m = folium.Map(location=[48.97, 11.5], zoom_start=7)
    cluster = MarkerCluster(
        options={"showCoverageOnHover": False},
        icon_create_function="""
        function(cluster){
            const count = cluster.getChildCount();
            const green = cluster.getAllChildMarkers().some(m=>m.options.icon.options.markerColor==='green');
            const color = green?'green':'red';
            return L.divIcon({html:`<div style='background:${color};border-radius:50%;width:32px;height:32px;display:flex;align-items:center;justify-content:center;color:white;'>${count}</div>`});
        }"""
    ).add_to(m)
    for _, r in df.iterrows():
        e = spaces.get(r["name"], {})
        color = "green" if e.get("space_name") else "red"
        popup = f"<b>{r['name']}</b><br><i>{r['type']}</i>"
        if e.get("space_name"):
            if e.get("contact"): popup += f"<br><b>Kontakt:</b> {e['contact']}"
            if e.get("email"): popup += f"<br><b>Email:</b> <a href='mailto:{e['email']}'>{e['email']}</a>"
            if e.get("website"): popup += f"<br><b>Web:</b> <a href='{e['website']}' target='_blank'>{e['website']}</a>"
            tools = ", ".join(e.get("tools", [])) or "–"
            popup += f"<hr style='margin:4px 0;'><i>{e['space_name']}</i><br>Werkzeuge: {tools}"
        else:
            popup += "<br><i>Kein Makerspace eingetragen.</i>"
        folium.Marker([r["lat"], r["lon"]],
                      icon=folium.Icon(color=color, icon="wrench", prefix="fa"),
                      popup=folium.Popup(popup, max_width=300)).add_to(cluster)
    Fullscreen().add_to(m)
    LocateControl().add_to(m)
    return m

st.markdown("### Karte")
st_folium(build_map(filtered_df, db), width=1280, height=650)
