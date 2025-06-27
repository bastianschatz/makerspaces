"""
Streamlit App – Makerspaces an bayerischen Gymnasien
===================================================

Version 2025‑06‑27‑c (Autofill‑Update)
-------------------------------------
* **Autofill**: Wählt man ein Gymnasium, werden vorhandene Makerspace‑Infos (Name, Werkzeuge, Ansprechpartner) automatisch in die Eingabefelder geladen → bequemes Aktualisieren statt Neu­eingabe.
* **Speichern‑Logik**: Ein Klick ersetzt den bisherigen Datensatz dieser Schule (statt ihn anzuhängen). Bei Schulen ohne Eintrag wird neu angelegt.
* Cluster‑Farbe (grün/rot) + Ansprechpartner‑Popup bleiben unverändert.

```bash
pip install streamlit folium streamlit-folium pandas requests
streamlit run app.py
```
"""
from __future__ import annotations

from pathlib import Path
import json
import re

import pandas as pd
import requests
import streamlit as st
import folium
from folium.plugins import MarkerCluster, Fullscreen, LocateControl
from streamlit_folium import st_folium

# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------
SCHOOL_CACHE = Path("schools_bavaria.csv")
SPACE_FILE = Path("makerspaces.json")
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# -----------------------------------------------------------------------------
# DATA LAYER
# -----------------------------------------------------------------------------
@st.cache_data(show_spinner="📡 Lade Gymnasien aus OpenStreetMap …")
def load_schools() -> pd.DataFrame:
    if SCHOOL_CACHE.exists():
        return pd.read_csv(SCHOOL_CACHE)

    query = r"""
    [out:json][timeout:120];
    area["ISO3166-2"="DE-BY"]->.searchArea;
    (
      node["amenity"="school"]["name"~"Gymnasium"](area.searchArea);
      way["amenity"="school"]["name"~"Gymnasium"](area.searchArea);
      relation["amenity"="school"]["name"~"Gymnasium"](area.searchArea);
    );
    out center tags;
    """

    resp = requests.post(OVERPASS_URL, data={"data": query})
    resp.raise_for_status()
    elements = resp.json()["elements"]

    rec: list[dict] = []
    for el in elements:
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        name = el.get("tags", {}).get("name")
        if lat and lon and name:
            rec.append({
                "name": re.sub(r"\s+Gymnasium$", " Gymnasium", name),
                "lat": lat,
                "lon": lon,
            })

    df = pd.DataFrame(rec).drop_duplicates()
    df.to_csv(SCHOOL_CACHE, index=False)
    return df


def load_makerspaces() -> dict[str, list[dict]]:
    return json.loads(SPACE_FILE.read_text()) if SPACE_FILE.exists() else {}


def save_makerspaces(db: dict[str, list[dict]]):
    SPACE_FILE.write_text(json.dumps(db, indent=2, ensure_ascii=False))


# -----------------------------------------------------------------------------
# UI LAYER
# -----------------------------------------------------------------------------

st.set_page_config(page_title="Makerspaces an bayerischen Gymnasien", layout="wide")

st.title("🛠️ Makerspaces an bayerischen Gymnasien")
st.caption("OpenStreetMap-Datenbasis · Erstellt mit Streamlit & Folium · Stand: automatisch aktuell")

schools_df = load_schools()
makerspaces_db = load_makerspaces()

# --- Sidebar form -------------------------------------------------------------
with st.sidebar:
    st.header("Makerspace erfassen / bearbeiten")

    chosen_school = st.selectbox("Gymnasium auswählen", schools_df["name"].sort_values())
    current_entry = makerspaces_db.get(chosen_school, [{}])[-1]  # letztes / einziges

    # Default‑Werte vorbereiten ----------------------------------------------
    default_space = current_entry.get("space_name", "")
    default_tools = ", ".join(current_entry.get("tools", [])) if current_entry else ""
    default_contact = current_entry.get("contact", "")

    # Eingabefelder -----------------------------------------------------------
    space_name = st.text_input("Bezeichnung des Makerspaces", value=default_space)
    tools_str = st.text_area("Werkzeuge (kommagetrennt)", value=default_tools)
    contact_name = st.text_input("Ansprechpartner (Name)", value=default_contact)

    if st.button("Speichern / Aktualisieren", type="primary"):
        if not space_name.strip():
            st.error("Bitte einen Namen für den Makerspace angeben.")
        else:
            new_entry = {
                "space_name": space_name.strip(),
                "tools": [t.strip() for t in tools_str.split(",") if t.strip()],
                "contact": contact_name.strip(),
            }
            makerspaces_db[chosen_school] = [new_entry]  # ersetze / lege an
            save_makerspaces(makerspaces_db)
            st.success("Eintrag gespeichert!")

# --- Map ----------------------------------------------------------------------

def make_map(df: pd.DataFrame, spaces: dict[str, list[dict]]) -> folium.Map:
    m = folium.Map(location=[48.97, 11.5], zoom_start=7, tiles="OpenStreetMap")

    # Cluster mit farbiger Bubble (rot/​grün) ----------------------------------
    cluster = MarkerCluster(
        name="Gymnasien",
        options={"showCoverageOnHover": False},
        icon_create_function="""
        function (cluster) {
            const count = cluster.getChildCount();
            const children = cluster.getAllChildMarkers();
            let hasSpace = false;
            for (let i = 0; i < children.length; i++) {
                if (children[i].options.icon.options.markerColor === 'green') {
                    hasSpace = true; break; }
            }
            const color = hasSpace ? 'green' : 'red';
            return L.divIcon({
                html: `<div style='background-color:${color};border-radius:50%;width:32px;height:32px;display:flex;align-items:center;justify-content:center;color:white;font-weight:bold;'>${count}</div>`
            });
        }
        """,
    ).add_to(m)

    for _, row in df.iterrows():
        entries = spaces.get(row["name"], [])
        has_space = bool(entries)
        icon_color = "green" if has_space else "red"

        popup_parts = [f"<b>{row['name']}</b>"]
        if entries and entries[0].get("contact"):
            popup_parts.append(f"<br><b>Ansprechpartner:</b> {entries[0]['contact']}")

        if has_space:
            for e in entries:
                tools = ", ".join(e["tools"]) if e["tools"] else "–"
                popup_parts.append(
                    "<hr style='margin:4px 0;'>" +
                    f"<i>{e['space_name']}</i><br>Werkzeuge: {tools}"
                )
        else:
            popup_parts.append("<br><i>Kein Makerspace eingetragen.</i>")

        folium.Marker(
            location=[row["lat"], row["lon"]],
            popup=folium.Popup("".join(popup_parts), max_width=300),
            icon=folium.Icon(color=icon_color, icon="wrench", prefix="fa"),
        ).add_to(cluster)

    Fullscreen(position="topright").add_to(m)
    LocateControl(auto_start=False).add_to(m)
    return m


st.markdown("### Interaktive Karte der Gymnasien")
st_folium(make_map(schools_df, makerspaces_db), width=1280, height=650)

st.caption("Datenquelle: © OpenStreetMap-Mitwirkende 2025 · Script unter MIT‑Lizenz")
