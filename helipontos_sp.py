#!/usr/bin/env python3
"""
Helipontos SP — Cruzamento de dados de helipontos da cidade de São Paulo.

Compara a base da Prefeitura (GeoSampa) com a base nacional (ANAC)
para identificar status de operação e discrepâncias.

Fontes:
  - ANAC: Portal de Dados Abertos (CSV de Aeródromos Privados / Helipontos)
  - GeoSampa: WFS da Prefeitura de São Paulo (camada de helipontos)
"""

import io
import logging
import re
import sys
import warnings
from pathlib import Path

import folium
import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import Point

warnings.filterwarnings("ignore", category=FutureWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CRS_WGS84 = "EPSG:4326"
CRS_UTM23S = "EPSG:31983"
BUFFER_METROS = 100

# ANAC download URLs — tried in order until one succeeds.
ANAC_URLS = [
    # V1 helipontos-specific CSV
    (
        "https://sistemas.anac.gov.br/dadosabertos/Aerodromos/"
        "Aer%C3%B3dromos%20Privados/Lista%20de%20aer%C3%B3dromos%20privados/"
        "Helipontos/Helipontos.csv"
    ),
    # V1 all private aerodromes CSV (includes helipontos)
    (
        "https://sistemas.anac.gov.br/dadosabertos/Aerodromos/"
        "Aer%C3%B3dromos%20Privados/Lista%20de%20aer%C3%B3dromos%20privados/"
        "Aerodromos%20Privados/AerodromosPrivados.csv"
    ),
    # Gov.br hosted copy
    (
        "https://www.gov.br/anac/pt-br/acesso-a-informacao/dados-abertos/"
        "areas-de-atuacao/aerodromos/aerodromos-privados/"
        "lista-de-aerodromos-privados-1/helipontos.csv/@@download/file"
    ),
]

# GeoSampa WFS — layer names to try in order.
GEOSAMPA_WFS_BASE = (
    "http://wfs.geosampa.prefeitura.sp.gov.br/geoserver/geoportal/ows"
)
GEOSAMPA_LAYER_NAMES = [
    "geoportal:GEOSAMPA_heliponto",
    "geoportal:riv_heliponto",
    "geoportal:heliponto",
]

# Output files
OUTPUT_CSV = "comparativo_helipontos_sp.csv"
OUTPUT_MAP = "mapa_helipontos_sp.html"

# ---------------------------------------------------------------------------
# 1. Coordinate conversion  (GMS → Decimal)
# ---------------------------------------------------------------------------


def dms_to_decimal(raw: str) -> float | None:
    """Convert a DMS (Graus Minutos Segundos) string to decimal degrees.

    Handles multiple ANAC formats:
      - ``023°35'10,0"S``   (typical V1)
      - ``023° 35' 10'' S`` (older format)
      - ``233510S``         (compact)
      - ``-23.585``         (already decimal)

    Returns a signed float (negative for S/W/O) or None on failure.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None

    text = str(raw).strip()
    if not text:
        return None

    # Already a simple float?
    try:
        val = float(text.replace(",", "."))
        return val
    except ValueError:
        pass

    # Determine hemisphere sign
    text_upper = text.upper()
    sign = -1 if any(h in text_upper for h in ("S", "W", "O")) else 1

    # Remove hemisphere letters and whitespace
    cleaned = re.sub(r"[NSWEWO]", "", text_upper).strip()

    # Try structured DMS: 023°35'10,0"  or  023° 35' 10''
    m = re.match(
        r"(\d{2,3})\s*[°º]\s*(\d{1,2})\s*['\u2019′]\s*"
        r"([\d,.]+)\s*(?:[\"''\u201D″]{1,2})?\s*$",
        cleaned,
    )
    if m:
        deg = int(m.group(1))
        minutes = int(m.group(2))
        sec = float(m.group(3).replace(",", "."))
        return sign * (deg + minutes / 60.0 + sec / 3600.0)

    # Compact format: 2335100  (DDMMSS.s packed) — 7 digits with tenths
    m = re.match(r"(\d{2,3})(\d{2})(\d{2}[,.]?\d*)", cleaned)
    if m:
        deg = int(m.group(1))
        minutes = int(m.group(2))
        sec = float(m.group(3).replace(",", "."))
        return sign * (deg + minutes / 60.0 + sec / 3600.0)

    log.warning("Could not parse coordinate: %r", raw)
    return None


# ---------------------------------------------------------------------------
# 2. Data acquisition — ANAC
# ---------------------------------------------------------------------------


def _download_with_retries(url: str, timeout: int = 30) -> requests.Response | None:
    """Try downloading *url* with simple retry logic."""
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200:
                return resp
            log.warning(
                "HTTP %s from %s (attempt %d)", resp.status_code, url, attempt + 1
            )
        except requests.RequestException as exc:
            log.warning("Request error for %s: %s (attempt %d)", url, exc, attempt + 1)
    return None


def load_anac(local_csv: str | None = None) -> gpd.GeoDataFrame:
    """Load ANAC heliport data, filter to São Paulo city, return GeoDataFrame."""

    df: pd.DataFrame | None = None

    # --- Try local file first ---
    if local_csv and Path(local_csv).exists():
        log.info("Loading ANAC data from local file: %s", local_csv)
        for sep in [";", ","]:
            try:
                df = pd.read_csv(local_csv, sep=sep, encoding="utf-8-sig")
                if len(df.columns) > 3:
                    break
            except Exception:
                continue

    # --- Try remote URLs ---
    if df is None or len(df.columns) <= 3:
        for url in ANAC_URLS:
            log.info("Trying ANAC URL: %s", url)
            resp = _download_with_retries(url)
            if resp is None:
                continue
            content = resp.content.decode("utf-8-sig", errors="replace")
            for sep in [";", ","]:
                try:
                    candidate = pd.read_csv(io.StringIO(content), sep=sep)
                    if len(candidate.columns) > 3:
                        df = candidate
                        log.info(
                            "Loaded ANAC data from URL (%d rows, %d cols)",
                            len(df),
                            len(df.columns),
                        )
                        break
                except Exception:
                    continue
            if df is not None and len(df.columns) > 3:
                break

    if df is None or df.empty:
        log.error(
            "Could not load ANAC data from any source. "
            "Place a local CSV as 'anac_helipontos.csv' and re-run."
        )
        sys.exit(1)

    # Normalise column names to uppercase for matching
    df.columns = [c.strip().upper() for c in df.columns]

    log.info("ANAC raw columns: %s", list(df.columns))
    log.info("ANAC raw rows: %d", len(df))

    # --- Identify key columns via heuristics ---
    col_map = _detect_anac_columns(df)
    log.info("ANAC column mapping: %s", col_map)

    # --- Filter to UF=SP, MUNICÍPIO=SÃO PAULO ---
    if col_map["uf"]:
        df = df[df[col_map["uf"]].astype(str).str.strip().str.upper() == "SP"]
    if col_map["municipio"]:
        df = df[
            df[col_map["municipio"]]
            .astype(str)
            .str.strip()
            .str.upper()
            .str.contains("S.O PAULO|SAO PAULO|SÃO PAULO", regex=True, na=False)
        ]

    log.info("ANAC rows after SP / São Paulo filter: %d", len(df))

    if df.empty:
        log.error("No ANAC records for São Paulo found.")
        sys.exit(1)

    # --- Parse coordinates ---
    lat_col = col_map["lat"]
    lon_col = col_map["lon"]
    df["lat_dec"] = df[lat_col].apply(dms_to_decimal)
    df["lon_dec"] = df[lon_col].apply(dms_to_decimal)

    # Ensure São Paulo coordinates are negative
    df["lat_dec"] = df["lat_dec"].apply(lambda v: -abs(v) if v and v != 0 else v)
    df["lon_dec"] = df["lon_dec"].apply(lambda v: -abs(v) if v and v != 0 else v)

    df = df.dropna(subset=["lat_dec", "lon_dec"])

    geometry = [Point(xy) for xy in zip(df["lon_dec"], df["lat_dec"])]
    gdf = gpd.GeoDataFrame(df, geometry=geometry, crs=CRS_WGS84)

    # Rename useful columns for downstream use
    rename = {}
    if col_map["nome"]:
        rename[col_map["nome"]] = "anac_nome"
    if col_map["oaci"]:
        rename[col_map["oaci"]] = "anac_oaci"
    if col_map["ciad"]:
        rename[col_map["ciad"]] = "anac_ciad"

    # Detect operation / status column
    status_col = col_map.get("operacao") or col_map.get("validade")
    if status_col:
        rename[status_col] = "anac_status_raw"

    gdf = gdf.rename(columns=rename)

    # Derive active/inactive flag
    gdf["anac_ativo"] = True  # default
    if "anac_status_raw" in gdf.columns:
        gdf["anac_ativo"] = ~gdf["anac_status_raw"].astype(str).str.upper().str.contains(
            "INATIV|CANCEL|REVOG|VENCID", na=False
        )
    # Also check registration validity date if available
    if col_map.get("validade") and col_map["validade"] not in rename:
        try:
            validade = pd.to_datetime(
                gdf[col_map["validade"]], dayfirst=True, errors="coerce"
            )
            expired = validade < pd.Timestamp.now()
            gdf.loc[expired, "anac_ativo"] = False
        except Exception:
            pass

    log.info("ANAC GeoDataFrame ready: %d features", len(gdf))
    return gdf


def _detect_anac_columns(df: pd.DataFrame) -> dict:
    """Heuristically map ANAC DataFrame columns to semantic roles."""
    cols = {c.upper(): c for c in df.columns}

    def _find(*candidates):
        for c in candidates:
            for col_upper, col_orig in cols.items():
                if c in col_upper:
                    return col_orig
        return None

    return {
        "lat": _find("LATITUDE", "LAT"),
        "lon": _find("LONGITUDE", "LONG", "LON"),
        "nome": _find("NOME"),
        "oaci": _find("OACI", "ICAO"),
        "ciad": _find("CIAD"),
        "uf": _find("UF"),
        "municipio": _find("MUNIC", "CIDADE"),
        "operacao": _find("OPERA"),
        "validade": _find("VALIDADE"),
    }


# ---------------------------------------------------------------------------
# 3. Data acquisition — GeoSampa
# ---------------------------------------------------------------------------


def load_geosampa(local_file: str | None = None) -> gpd.GeoDataFrame:
    """Load GeoSampa heliport data from WFS or local file."""

    gdf: gpd.GeoDataFrame | None = None

    # --- Local file ---
    if local_file and Path(local_file).exists():
        log.info("Loading GeoSampa data from local file: %s", local_file)
        try:
            gdf = gpd.read_file(local_file)
        except Exception as exc:
            log.warning("Failed to read local GeoSampa file: %s", exc)

    # --- WFS ---
    if gdf is None:
        for layer in GEOSAMPA_LAYER_NAMES:
            wfs_url = (
                f"{GEOSAMPA_WFS_BASE}?service=WFS&version=1.0.0"
                f"&request=GetFeature&typeName={layer}"
                f"&outputFormat=application%2Fjson"
            )
            log.info("Trying GeoSampa WFS layer: %s", layer)
            try:
                resp = _download_with_retries(wfs_url, timeout=60)
                if resp is None:
                    continue
                data = resp.json()
                if "features" in data and len(data["features"]) > 0:
                    gdf = gpd.GeoDataFrame.from_features(
                        data["features"], crs=CRS_UTM23S
                    )
                    log.info(
                        "Loaded %d features from GeoSampa layer %s",
                        len(gdf),
                        layer,
                    )
                    break
                else:
                    log.warning("Layer %s returned 0 features or bad response", layer)
            except Exception as exc:
                log.warning("Error fetching layer %s: %s", layer, exc)

    if gdf is None or gdf.empty:
        log.error(
            "Could not load GeoSampa data. "
            "Place a local GeoJSON/Shapefile as 'helipontos.geojson' and re-run."
        )
        sys.exit(1)

    # Ensure WGS84
    if gdf.crs and gdf.crs != CRS_WGS84:
        gdf = gdf.to_crs(CRS_WGS84)

    # Normalise column names
    gdf.columns = [c.strip().lower() for c in gdf.columns]

    # Identify name column
    name_col = None
    for candidate in [
        "nm_empreendimento_heliponto",
        "nm_empreendimento",
        "nome",
        "name",
        "nm_heliponto",
    ]:
        if candidate in gdf.columns:
            name_col = candidate
            break
    if name_col and name_col != "gs_nome":
        gdf = gdf.rename(columns={name_col: "gs_nome"})

    # Identify address column
    addr_col = None
    for candidate in [
        "nm_logradouro_heliponto",
        "tx_endereco_heliponto",
        "endereco",
        "logradouro",
    ]:
        if candidate in gdf.columns:
            addr_col = candidate
            break
    if addr_col and addr_col != "gs_endereco":
        gdf = gdf.rename(columns={addr_col: "gs_endereco"})

    # Identify status column from GeoSampa
    status_col = None
    for candidate in [
        "st_deferimento_processo_administrativo",
        "st_deferimento",
        "situacao",
        "status",
    ]:
        if candidate in gdf.columns:
            status_col = candidate
            break
    if status_col and status_col != "gs_situacao":
        gdf = gdf.rename(columns={status_col: "gs_situacao"})

    log.info("GeoSampa GeoDataFrame ready: %d features", len(gdf))
    log.info("GeoSampa columns: %s", list(gdf.columns))
    return gdf


# ---------------------------------------------------------------------------
# 4. Spatial join & status consolidation
# ---------------------------------------------------------------------------


def spatial_join(
    gdf_geosampa: gpd.GeoDataFrame,
    gdf_anac: gpd.GeoDataFrame,
    buffer_m: int = BUFFER_METROS,
) -> gpd.GeoDataFrame:
    """Cross-reference GeoSampa and ANAC by proximity (nearest within buffer)."""

    # Project to UTM for metre-based distance
    gs = gdf_geosampa.to_crs(CRS_UTM23S).copy()
    anac = gdf_anac.to_crs(CRS_UTM23S).copy()

    # --- 1. Match GeoSampa → nearest ANAC ---
    joined = gpd.sjoin_nearest(
        gs,
        anac,
        how="left",
        max_distance=buffer_m,
        distance_col="dist_metros",
    )

    # sjoin_nearest may produce duplicates; keep closest match per GeoSampa row
    joined = joined.sort_values("dist_metros").drop_duplicates(
        subset=[gs.index.name or "index"], keep="first"
    )

    # --- 2. Identify ANAC records with no GeoSampa match ---
    matched_anac_idx = set(
        joined.dropna(subset=["dist_metros"])["index_right"].dropna().astype(int)
    )
    unmatched_anac = anac.loc[~anac.index.isin(matched_anac_idx)].copy()

    # --- 3. Assign consolidated status ---
    def _status(row):
        if pd.isna(row.get("dist_metros")):
            return "NÃO_CADASTRADO_ANAC"
        if not row.get("anac_ativo", True):
            return "DIVERGENTE_ANAC_INATIVO"
        return "REGULAR"

    joined["status_consolidado"] = joined.apply(_status, axis=1)

    # Add unmatched ANAC rows as NÃO_CADASTRADO_PREFEITURA
    if not unmatched_anac.empty:
        unmatched_anac["status_consolidado"] = "NÃO_CADASTRADO_PREFEITURA"
        # Ensure compatible columns
        for col in joined.columns:
            if col not in unmatched_anac.columns:
                unmatched_anac[col] = None
        joined = pd.concat(
            [joined, unmatched_anac[joined.columns]], ignore_index=True
        )

    # Back to WGS84 for display
    joined = gpd.GeoDataFrame(joined, geometry="geometry", crs=CRS_UTM23S)
    joined = joined.to_crs(CRS_WGS84)

    log.info("Spatial join complete: %d total records", len(joined))
    for status, count in joined["status_consolidado"].value_counts().items():
        log.info("  %s: %d", status, count)

    return joined


# ---------------------------------------------------------------------------
# 5. Map visualisation (Folium)
# ---------------------------------------------------------------------------

STATUS_COLORS = {
    "REGULAR": "green",
    "DIVERGENTE_ANAC_INATIVO": "orange",
    "NÃO_CADASTRADO_ANAC": "red",
    "NÃO_CADASTRADO_PREFEITURA": "purple",
}


def build_map(gdf: gpd.GeoDataFrame, output_path: str = OUTPUT_MAP) -> None:
    """Create an interactive Folium map of heliport statuses."""

    # Centre on São Paulo
    center = [-23.55, -46.63]
    m = folium.Map(location=center, zoom_start=12, tiles="CartoDB positron")

    # Legend HTML
    legend_html = """
    <div style="position: fixed; bottom: 30px; left: 30px; z-index: 1000;
         background: white; padding: 12px 16px; border-radius: 8px;
         box-shadow: 0 2px 6px rgba(0,0,0,0.3); font-size: 13px;
         font-family: Arial, sans-serif;">
      <b>Status do Heliponto</b><br>
      <i style="background:green;width:12px;height:12px;display:inline-block;
         border-radius:50%;margin-right:6px;"></i> Regular<br>
      <i style="background:orange;width:12px;height:12px;display:inline-block;
         border-radius:50%;margin-right:6px;"></i> Inativo na ANAC<br>
      <i style="background:red;width:12px;height:12px;display:inline-block;
         border-radius:50%;margin-right:6px;"></i> Não cadastrado ANAC<br>
      <i style="background:purple;width:12px;height:12px;display:inline-block;
         border-radius:50%;margin-right:6px;"></i> Não cadastrado Prefeitura<br>
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))

    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        lat, lon = geom.y, geom.x
        status = row.get("status_consolidado", "UNKNOWN")
        color = STATUS_COLORS.get(status, "gray")

        nome = row.get("gs_nome") or row.get("anac_nome") or "Sem nome"
        oaci = row.get("anac_oaci", "N/A")
        dist = row.get("dist_metros")
        dist_str = f"{dist:.0f}m" if pd.notna(dist) else "N/A"

        popup_html = (
            f"<b>{nome}</b><br>"
            f"OACI: {oaci}<br>"
            f"Status: {status}<br>"
            f"Distância match: {dist_str}"
        )

        folium.CircleMarker(
            location=[lat, lon],
            radius=7,
            color=color,
            fill=True,
            fill_color=color,
            fill_opacity=0.8,
            popup=folium.Popup(popup_html, max_width=300),
            tooltip=nome,
        ).add_to(m)

    m.save(output_path)
    log.info("Map saved to %s", output_path)


# ---------------------------------------------------------------------------
# 6. CSV export
# ---------------------------------------------------------------------------


def export_csv(gdf: gpd.GeoDataFrame, output_path: str = OUTPUT_CSV) -> None:
    """Export the consolidated result to CSV."""

    # Select and order key columns
    key_cols = [
        "gs_nome",
        "gs_endereco",
        "gs_situacao",
        "anac_nome",
        "anac_oaci",
        "anac_ciad",
        "anac_ativo",
        "status_consolidado",
        "dist_metros",
    ]
    available = [c for c in key_cols if c in gdf.columns]

    # Add latitude/longitude
    export = gdf[available].copy()
    export["latitude"] = gdf.geometry.y
    export["longitude"] = gdf.geometry.x

    export.to_csv(output_path, index=False, encoding="utf-8-sig")
    log.info("CSV exported to %s (%d rows)", output_path, len(export))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Cruzamento de helipontos SP — GeoSampa × ANAC"
    )
    parser.add_argument(
        "--anac-csv",
        default=None,
        help=(
            "Path to a local ANAC CSV file. "
            "If omitted the script tries to download from ANAC servers."
        ),
    )
    parser.add_argument(
        "--geosampa-file",
        default=None,
        help=(
            "Path to a local GeoSampa GeoJSON/Shapefile. "
            "If omitted the script tries the WFS endpoint."
        ),
    )
    parser.add_argument(
        "--buffer",
        type=int,
        default=BUFFER_METROS,
        help="Buffer distance in metres for spatial join (default: 100).",
    )
    parser.add_argument(
        "--output-csv",
        default=OUTPUT_CSV,
        help="Output CSV path (default: comparativo_helipontos_sp.csv).",
    )
    parser.add_argument(
        "--output-map",
        default=OUTPUT_MAP,
        help="Output HTML map path (default: mapa_helipontos_sp.html).",
    )
    args = parser.parse_args()

    # --- Auto-detect local fallback files ---
    anac_local = args.anac_csv
    if not anac_local:
        for candidate in [
            "anac_helipontos.csv",
            "AerodromosPrivados.csv",
            "Helipontos.csv",
        ]:
            if Path(candidate).exists():
                anac_local = candidate
                break

    geosampa_local = args.geosampa_file
    if not geosampa_local:
        for candidate in [
            "helipontos.geojson",
            "helipontos.json",
            "helipontos.shp",
            "geosampa_helipontos.geojson",
        ]:
            if Path(candidate).exists():
                geosampa_local = candidate
                break

    log.info("=" * 60)
    log.info("Helipontos SP — GeoSampa × ANAC")
    log.info("=" * 60)

    # Step 1–2: Load and clean data
    gdf_anac = load_anac(local_csv=anac_local)
    gdf_geosampa = load_geosampa(local_file=geosampa_local)

    # Step 3: Spatial join
    gdf_result = spatial_join(gdf_geosampa, gdf_anac, buffer_m=args.buffer)

    # Step 4: Outputs
    build_map(gdf_result, output_path=args.output_map)
    export_csv(gdf_result, output_path=args.output_csv)

    log.info("=" * 60)
    log.info("Done! Files generated:")
    log.info("  CSV: %s", args.output_csv)
    log.info("  Map: %s", args.output_map)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
