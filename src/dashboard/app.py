"""
Streamlit dashboard for ForestPulse MVP.

Displays disturbance polygons on a Folium map with confidence filtering
and date-range slider.
"""

import streamlit as st
import requests
import folium
from streamlit_folium import st_folium
import pandas as pd

API_BASE = "http://localhost:8000"

st.set_page_config(page_title="ForestPulse MVP", layout="wide")
st.title("🌲 ForestPulse — Harz Disturbance Monitor 2019")
st.caption("Sentinel-2 harmonic residual detection · June–September 2019 · Confidence-flagged polygons")

# --- Sidebar controls ---
with st.sidebar:
    st.header("Filters")
    conf_options = st.multiselect(
        "Confidence",
        ["high", "sparse", "low", "none"],
        default=["high", "sparse"],
    )
    min_area = st.slider("Minimum area (ha)", 0.0, 10.0, 0.5, 0.1)

    st.markdown("---")
    st.subheader("API stats")
    try:
        stats = requests.get(f"{API_BASE}/stats", timeout=5).json()
        for s in stats:
            st.write(f"**{s['confidence']}**: {s['count']} polygons, {s['total_ha']} ha")
    except Exception as e:
        st.error(f"API not reachable: {e}")

# --- Fetch data ---
def fetch_disturbances(confidence_list, min_area):
    all_features = []
    for conf in confidence_list:
        try:
            r = requests.get(
                f"{API_BASE}/disturbances",
                params={"confidence": conf, "min_area_ha": min_area},
                timeout=10,
            )
            data = r.json()
            all_features.extend(data.get("features", []))
        except Exception as e:
            st.warning(f"Failed to fetch {conf}: {e}")
    return all_features

features = fetch_disturbances(conf_options, min_area)
st.sidebar.info(f"Showing {len(features)} polygons")

# --- Build map ---
m = folium.Map(location=[51.75, 10.65], zoom_start=11, tiles="OpenStreetMap")

color_map = {"high": "#d73027", "sparse": "#fc8d59", "low": "#fee090", "none": "#cccccc"}

for f in features:
    geom = f["geometry"]
    props = f["properties"]
    conf = props.get("confidence", "none")
    folium.GeoJson(
        geom,
        style_function=lambda x, c=conf: {
            "fillColor": color_map.get(c, "#cccccc"),
            "color": "black",
            "weight": 0.5,
            "fillOpacity": 0.6,
        },
        tooltip=folium.Tooltip(
            f"Date: {props.get('detection_date')}<br>"
            f"Confidence: {conf}<br>"
            f"Area: {props.get('area_ha')} ha<br>"
            f"Obs density (60d): {props.get('obs_density_60d')}<br>"
            f"Max gap: {props.get('max_gap_days')} days"
        ),
    ).add_to(m)

st_folium(m, width=1200, height=700)

# --- Table below map ---
if features:
    df = pd.DataFrame([f["properties"] for f in features])
    st.subheader("Disturbance table")
    st.dataframe(df.sort_values("area_ha", ascending=False), use_container_width=True)