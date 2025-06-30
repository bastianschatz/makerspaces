from __future__ import annotations

import json, os, re, hashlib
from pathlib import Path
from textwrap import dedent

import pandas as pd
import requests
import streamlit as st
import folium
from folium.plugins import MarkerCluster, Fullscreen, LocateControl
from streamlit_folium import st_folium

# -------------------------------------------------------------------
# Konfiguration
# -------------------------------------------------------------------
PERSIST_DIR     = Path("/mount/src")

SCHOOL_CACHE    = PERSIST_DIR / "schools_bavaria.csv"
SPACE_FILE      = PERSIST_DIR / "makerspaces.json"
MEDIEN_CACHE    = PERSIST_DIR / "medienzentren.csv"
OVERPASS_URL   = "https://overpass-api.de/api/interpreter"

# Passwort (Env > secrets.toml > Fallback)
_env_pw   = os.getenv("MAKERSPACE_ADMIN_PW")
try:
    _secret_pw = st.secrets["makerspace_admin_pw"]
except Exception:
    _secret_pw = None
ADMIN_PASSWORD = _env_pw or _secret_pw or "changeme"

# -------------------------------------------------------------------
# Helfer
# -------------------------------------------------------------------
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
    L = name.lower()
    for typ, pat in patterns.items():
        if re.search(pat, L):
            return typ
    return "Sonstige"

# -------------------------------------------------------------------
# Schuldaten laden (CSV-Cache)
# -------------------------------------------------------------------
@st.cache_data(show_spinner="📡 Lade Schulen …")
def load_schools() -> pd.DataFrame:
    if SCHOOL_CACHE.exists():
        df = pd.read_csv(SCHOOL_CACHE)
        if "type" not in df.columns:
            df["type"] = df["name"].apply(school_type_from_name)
            df.to_csv(SCHOOL_CACHE, index=False)
        return df

    query = dedent("""
        [out:json][timeout:120];
        area["ISO3166-2"="DE-BY"]->.searchArea;
        (
          node["amenity"="school"](area.searchArea);
          way ["amenity"="school"](area.searchArea);
          relation["amenity"="school"](area.searchArea);
        );
        out center tags;
    """)
    els = requests.post(OVERPASS_URL, data={"data": query}).json()["elements"]
    rows = []
    for el in els:
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        name = el.get("tags", {}).get("name")
        if lat and lon and name:
            rows.append({"name": name, "lat": lat, "lon": lon,
                         "type": school_type_from_name(name)})
    df = pd.DataFrame(rows).drop_duplicates()
    df.to_csv(SCHOOL_CACHE, index=False)
    return df

# -------------------------------------------------------------------
# Makerspace-DB laden / initialisieren
# -------------------------------------------------------------------
def load_db(schools: pd.DataFrame) -> dict[str, dict]:
    raw = json.loads(SPACE_FILE.read_text()) if SPACE_FILE.exists() else {}
    db  = {k: (v[0] if isinstance(v, list) else v) for k, v in raw.items()}
    for n in schools["name"]:
        db.setdefault(n, {})
    SPACE_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2))
    return db

# -------------------------------------------------------------------
# Streamlit UI
# -------------------------------------------------------------------
st.set_page_config(page_title="Makerspaces Bayern", layout="wide")
st.title("🛠️ Makerspaces an Schulen in Bayern")

schools_df = load_schools()
db         = load_db(schools_df)

# ---- Sidebar -------------------------------------------------------
with st.sidebar:
    st.header("Filter & Verwaltung")
    sel_types = st.multiselect(
        "Schularten",
        sorted(schools_df["type"].unique()),
        default=[],
    )
    filtered_df = (schools_df
                   if not sel_types
                   else schools_df[schools_df["type"].isin(sel_types)])

    st.divider()
    st.subheader("Makerspace bearbeiten")

    school = st.selectbox("Schule wählen",
                          filtered_df["name"].sort_values())
    entry  = db.get(school, {})

    space   = st.text_input("Makerspace-Name", entry.get("space_name", ""))
    tools   = st.text_area("Werkzeuge (kommagetrennt)",
                           ", ".join(entry.get("tools", [])),
                           height=160)
    contact = st.text_input("Ansprechpartner", entry.get("contact", ""))
    email   = st.text_input("E-Mail",          entry.get("email",   ""))
    site    = st.text_input("Webseite",        entry.get("website", ""))

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Speichern"):
            db[school] = {
                "space_name": space.strip(),
                "tools"     : [t.strip() for t in tools.split(",") if t.strip()],
                "contact"   : contact.strip(),
                "email"     : email.strip(),
                "website"   : site.strip(),
            }
            SPACE_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2))
            st.session_state.pop("map_key", None)
            st.success("Gespeichert ✓")
    with col2:
        if entry.get("space_name"):
            pw = st.text_input("Passwort", type="password")
            if st.button("Löschen") and pw == ADMIN_PASSWORD:
                db[school] = {}
                SPACE_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2))
                st.session_state.pop("map_key", None)
                st.success("Gelöscht 🗑️")

# -------------------------------------------------------------------
# Karte bauen
# -------------------------------------------------------------------
def build_map(df: pd.DataFrame, spaces: dict[str, dict]) -> folium.Map:
    m = folium.Map(location=[48.97, 11.5], zoom_start=7)

    cluster = MarkerCluster(
        options=dict(showCoverageOnHover=False, chunkedLoading=True),
        icon_create_function="""
        function(c){
          const green = c.getAllChildMarkers().some(m=>m.options.hasSpace);
          const col   = green ? 'green' : 'red';
          return L.divIcon({html:`<div style='background:${col};border-radius:50%;width:32px;height:32px;display:flex;align-items:center;justify-content:center;color:white;font-weight:bold;'>${c.getChildCount()}</div>`});
        }"""
    ).add_to(m)

    for _, r in df.iterrows():
        info      = spaces.get(r["name"], {})
        has_space = bool(info.get("space_name"))
        color     = "green" if has_space else "red"

        pop  = [f"<b>{r['name']}</b>", f"<br><i>{r['type']}</i>"]
        if has_space:
            if info.get("contact"):
                pop.append(f"<br><b>Kontakt:</b> {info['contact']}")
            if info.get("email"):
                pop.append(f"<br><b>Email:</b> <a href='mailto:{info['email']}'>{info['email']}</a>")
            if info.get("website"):
                pop.append(f"<br><b>Web:</b> <a href='{info['website']}' target='_blank'>{info['website']}</a>")
            tools = ", ".join(info.get("tools", [])) or "–"
            pop.append(f"<hr style='margin:4px 0;'><i>{info['space_name']}</i><br>Werkzeuge: {tools}")
        else:
            pop.append("<br><i>Kein Makerspace eingetragen.</i>")

        mk = folium.CircleMarker([r["lat"], r["lon"]],
                                 radius=6, color=color,
                                 fill=True, fillColor=color, fillOpacity=0.9)
        mk.options["hasSpace"] = has_space
        mk.add_child(folium.Popup("".join(pop), max_width=300))
        mk.add_to(cluster)

    Fullscreen().add_to(m)
    LocateControl().add_to(m)
    return m

# -------------------------------------------------------------------
# Karte anzeigen (Session-Cache)
# -------------------------------------------------------------------
def map_cache_key(df: pd.DataFrame) -> str:
    h = hashlib.md5(pd.util.hash_pandas_object(df["name"], index=False).values).hexdigest()
    mtime = os.path.getmtime(SPACE_FILE) if SPACE_FILE.exists() else 0
    return f"{h}_{mtime}"

if sel_types:
    key = map_cache_key(filtered_df)
    if st.session_state.get("map_key") != key:
        st.session_state["map_obj"] = build_map(filtered_df, db)
        st.session_state["map_key"] = key
    st_folium(st.session_state["map_obj"], width=1280, height=650)
else:
    st.info("Bitte mindestens eine Schulart auswählen.")