"""
FastAPI endpoint for forest disturbance polygons.

Reads from PostGIS and returns GeoJSON with optional confidence filtering.
"""

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from geoalchemy2.shape import to_shape
import json

DATABASE_URL = "postgresql://forest:forestpulse@localhost:5432/disturbances"
engine = create_engine(DATABASE_URL)

app = FastAPI(title="ForestPulse MVP API", version="0.1.0")


@app.get("/")
def root():
    return {"service": "ForestPulse MVP", "endpoints": ["/disturbances", "/health"]}


@app.get("/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1;"))
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(e)})


@app.get("/disturbances")
def get_disturbances(
    confidence: str | None = Query(None, description="Filter: high, sparse, low, none"),
    min_area_ha: float = Query(0.0, description="Minimum polygon area in hectares"),
    start_date: str | None = Query(None, description="YYYY-MM-DD lower bound"),
    end_date: str | None = Query(None, description="YYYY-MM-DD upper bound"),
):
    """
    Return disturbance polygons as GeoJSON FeatureCollection.
    """
    where = ["area_ha >= :min_area"]
    params = {"min_area": min_area_ha}

    if confidence:
        where.append("confidence = :conf")
        params["conf"] = confidence
    if start_date:
        where.append("detection_date >= :start")
        params["start"] = start_date
    if end_date:
        where.append("detection_date <= :end")
        params["end"] = end_date

    sql = text(f"""
        SELECT id, detection_date, confidence, obs_density_60d,
               max_gap_days, area_ha,
               ST_AsGeoJSON(ST_Transform(geom, 4326)) AS geojson
        FROM disturbance_polygons
        WHERE {' AND '.join(where)}
        ORDER BY detection_date;
    """)

    with engine.connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    features = []
    for r in rows:
        features.append({
            "type": "Feature",
            "geometry": json.loads(r.geojson),
            "properties": {
                "id": r.id,
                "detection_date": str(r.detection_date) if r.detection_date else None,
                "confidence": r.confidence,
                "obs_density_60d": r.obs_density_60d,
                "max_gap_days": r.max_gap_days,
                "area_ha": r.area_ha,
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
        "count": len(features),
    }


@app.get("/stats")
def get_stats():
    """Summary statistics for dashboard header."""
    sql = text("""
        SELECT confidence, COUNT(*) AS n, ROUND(SUM(area_ha)::numeric, 1) AS total_ha
        FROM disturbance_polygons
        GROUP BY confidence;
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [
        {"confidence": r.confidence, "count": r.n, "total_ha": float(r.total_ha or 0)}
        for r in rows
    ]