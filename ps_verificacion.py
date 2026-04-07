"""
Presentación Simplificada — Verificación cruzada GA4 + DGR
==========================================================
Extrae eventos de presentación simplificada de GA4,
deduplica, verifica contra DGR Gestión y genera reporte HTML.

Uso:
    python ps_verificacion.py -c credenciales.json -u USER_DGR -p PASS_DGR
    python ps_verificacion.py -c credenciales.json -u USER_DGR -p PASS_DGR --desde 2026-01-01 --hasta 2026-03-31
    python ps_verificacion.py -c credenciales.json --solo-ga4  # sin verificación DGR

Requiere:
    pip install google-analytics-data google-auth pandas requests
"""

import argparse
import io
import json
import re
import sys
import time
from datetime import datetime
from dateutil.relativedelta import relativedelta
from html.parser import HTMLParser
from pathlib import Path

# Forzar UTF-8 en stdout/stderr para consolas Windows (cp1252)
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import pandas as pd
import requests
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Filter,
    FilterExpression,
    FilterExpressionList,
    Metric,
    RunReportRequest,
)
from google.oauth2.service_account import Credentials

# ── Constantes ────────────────────────────────────────────────
PROPERTY_ID = "485388348"
SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]

GA4_EVENTS = [
    "PS_boton_presentar_y_salir",
    "PS_boton_presentar_y_generar_pago",
]
GA4_HOSTNAME = "servicios.comarb.gob.ar"

DGR_BASE = "https://dgrgw.comarb.gob.ar/dgr"
DGR_LOGIN_URL = f"{DGR_BASE}/j_security_check"
DGR_SEARCH_URL = f"{DGR_BASE}/sfrwDdjj.do"


# ═══════════════════════════════════════════════════════════════
# PASO 1: Extracción de datos GA4
# ═══════════════════════════════════════════════════════════════

def extract_ga4_data(creds_path: str, start_date: str, end_date: str) -> tuple[pd.DataFrame, dict]:
    """Extrae eventos de presentación simplificada de GA4.
    Retorna (df_principal, chart_data) donde chart_data tiene las series para gráficos."""
    credentials = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    client = BetaAnalyticsDataClient(credentials=credentials)

    # Filtro: hostname = servicios.comarb.gob.ar
    hostname_filter = FilterExpression(
        filter=Filter(
            field_name="hostName",
            string_filter=Filter.StringFilter(
                value=GA4_HOSTNAME,
                match_type=Filter.StringFilter.MatchType.EXACT,
            ),
        )
    )

    # Filtro: eventName IN [PS_boton_presentar_y_salir, PS_boton_presentar_y_generar_pago]
    event_filter = FilterExpression(
        or_group=FilterExpressionList(
            expressions=[
                FilterExpression(
                    filter=Filter(
                        field_name="eventName",
                        string_filter=Filter.StringFilter(
                            value=ev,
                            match_type=Filter.StringFilter.MatchType.EXACT,
                        ),
                    )
                )
                for ev in GA4_EVENTS
            ]
        )
    )

    # Combinar con AND
    combined_filter = FilterExpression(
        and_group=FilterExpressionList(
            expressions=[hostname_filter, event_filter]
        )
    )

    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[
            Dimension(name="customEvent:CUIT"),
            Dimension(name="customEvent:exact_timestamp"),
            Dimension(name="eventName"),
            Dimension(name="region"),
            Dimension(name="customEvent:Total"),
        ],
        metrics=[
            Metric(name="eventCount"),
        ],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimension_filter=combined_filter,
        limit=10000,
    )

    print("  📡 Consultando GA4 (presentaciones)...")
    response = client.run_report(request)

    dim_names = ["cuit", "exact_timestamp", "nombre_evento", "region", "total"]
    rows = []
    for row in response.rows:
        record = {}
        for i, dv in enumerate(row.dimension_values):
            record[dim_names[i]] = dv.value
        for i, mv in enumerate(row.metric_values):
            record["numero_eventos"] = mv.value
        rows.append(record)

    df = pd.DataFrame(rows)
    if df.empty:
        print("  ⚠️  No se encontraron datos para el período indicado.")
        return df, {}

    df["numero_eventos"] = pd.to_numeric(df["numero_eventos"], errors="coerce")

    # Limpiar (not set) y vacíos
    df = df[~df["cuit"].isin(["(not set)", ""])].copy()
    df = df[df["cuit"].str.strip() != ""].copy()

    print(f"  ✅ {len(df)} registros de presentación extraídos")

    # ── Query 2: Encuesta (PS_boton_enviar_encuesta) ──
    print("  📡 Consultando GA4 (encuestas)...")

    encuesta_filter = FilterExpression(
        and_group=FilterExpressionList(
            expressions=[
                hostname_filter,
                FilterExpression(
                    filter=Filter(
                        field_name="eventName",
                        string_filter=Filter.StringFilter(
                            value="PS_boton_enviar_encuesta",
                            match_type=Filter.StringFilter.MatchType.EXACT,
                        ),
                    )
                ),
            ]
        )
    )

    enc_request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[
            Dimension(name="customEvent:CUIT"),
            Dimension(name="customEvent:exact_timestamp"),
            Dimension(name="customEvent:estrellas_valor"),
            Dimension(name="customEvent:texto_feedback"),
        ],
        metrics=[Metric(name="eventCount")],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimension_filter=encuesta_filter,
        limit=10000,
    )

    enc_response = client.run_report(enc_request)

    enc_rows = []
    for row in enc_response.rows:
        vals = [dv.value for dv in row.dimension_values]
        enc_rows.append({
            "cuit": vals[0],
            "exact_timestamp": vals[1],
            "estrellas_valor": vals[2],
            "texto_feedback": vals[3],
        })

    df_enc = pd.DataFrame(enc_rows)

    if df_enc.empty:
        df["estrellas_valor"] = ""
        df["texto_feedback"] = ""
        print("  ⚠️  No se encontraron datos de encuesta.")
    else:
        # Limpiar (not set)
        for col in ["estrellas_valor", "texto_feedback"]:
            df_enc[col] = df_enc[col].replace("(not set)", "")
        df_enc = df_enc[~df_enc["cuit"].isin(["(not set)", ""])].copy()

        # Cruzar por CUIT + fecha (sin hora)
        df["_fecha_cruce"] = pd.to_datetime(df["exact_timestamp"], errors="coerce").dt.date
        df_enc["_fecha_cruce"] = pd.to_datetime(df_enc["exact_timestamp"], errors="coerce").dt.date

        # Tomar la última encuesta del día por CUIT
        df_enc = df_enc.sort_values("exact_timestamp", ascending=False)
        df_enc = df_enc.drop_duplicates(subset=["cuit", "_fecha_cruce"], keep="first")

        df = df.merge(
            df_enc[["cuit", "_fecha_cruce", "estrellas_valor", "texto_feedback"]],
            on=["cuit", "_fecha_cruce"],
            how="left",
        )
        df["estrellas_valor"] = df["estrellas_valor"].fillna("")
        df["texto_feedback"] = df["texto_feedback"].fillna("")
        df.drop(columns=["_fecha_cruce"], inplace=True)

        n_enc = (df["estrellas_valor"] != "").sum()
        print(f"  ✅ {n_enc} registros con datos de encuesta cruzados")

    # ── Query 3: Encuestas cerradas (PS_cerrar_encuesta) — solo para gráficos ──
    print("  📡 Consultando GA4 (encuestas cerradas)...")

    cerrar_filter = FilterExpression(
        and_group=FilterExpressionList(
            expressions=[
                hostname_filter,
                FilterExpression(
                    filter=Filter(
                        field_name="eventName",
                        string_filter=Filter.StringFilter(
                            value="PS_cerrar_encuesta",
                            match_type=Filter.StringFilter.MatchType.EXACT,
                        ),
                    )
                ),
            ]
        )
    )

    cerrar_request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name="date")],
        metrics=[Metric(name="eventCount")],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimension_filter=cerrar_filter,
        limit=10000,
    )

    cerrar_response = client.run_report(cerrar_request)
    cerrar_por_dia = {}
    for row in cerrar_response.rows:
        d = row.dimension_values[0].value  # YYYYMMDD
        cerrar_por_dia[f"{d[:4]}-{d[4:6]}-{d[6:8]}"] = int(row.metric_values[0].value)

    # ── Construir series diarias para gráficos ──
    df["_fecha_ts"] = pd.to_datetime(df["exact_timestamp"], errors="coerce").dt.strftime("%Y-%m-%d")
    presentadas_por_dia = df.groupby("_fecha_ts")["numero_eventos"].sum().to_dict()
    df.drop(columns=["_fecha_ts"], inplace=True)

    # Encuestas enviadas por día (del df_enc ya obtenido)
    enviadas_por_dia = {}
    if not df_enc.empty:
        df_enc["_fecha_ts"] = pd.to_datetime(df_enc["exact_timestamp"], errors="coerce").dt.strftime("%Y-%m-%d")
        enviadas_por_dia = df_enc.groupby("_fecha_ts").size().to_dict()

    # Valoraciones (estrellas)
    estrellas_dist = {}
    if not df_enc.empty:
        est_vals = df_enc["estrellas_valor"].replace("", pd.NA).dropna()
        estrellas_dist = est_vals.value_counts().to_dict()

    # Textos de feedback
    feedback_textos = []
    if not df_enc.empty:
        fb = df_enc["texto_feedback"].replace("", pd.NA).dropna().tolist()
        feedback_textos = [t for t in fb if t and t != "(not set)"]

    # Todas las fechas del rango
    all_dates = sorted(set(
        list(presentadas_por_dia.keys())
        + list(enviadas_por_dia.keys())
        + list(cerrar_por_dia.keys())
    ))

    chart_data = {
        "fechas": all_dates,
        "presentadas": [int(presentadas_por_dia.get(d, 0)) for d in all_dates],
        "enc_enviadas": [int(enviadas_por_dia.get(d, 0)) for d in all_dates],
        "enc_cerradas": [int(cerrar_por_dia.get(d, 0)) for d in all_dates],
        "presentadas_no_dup": [],  # se llena después de deduplicar en main
        "estrellas": estrellas_dist,
        "feedback": feedback_textos,
    }

    print(f"  ✅ Datos de gráficos: {len(all_dates)} días, {len(feedback_textos)} textos de feedback")

    return df, chart_data


# ═══════════════════════════════════════════════════════════════
# PASO 2: Deduplicación
# ═══════════════════════════════════════════════════════════════

def deduplicate(df: pd.DataFrame) -> pd.DataFrame:
    """
    PARTITION BY cuit, CAST(exact_timestamp AS DATE)
    ORDER BY exact_timestamp DESC
    ROW_NUMBER() = 1 → 'No' (último del día, no duplicado)
    Resto → 'Sí' (duplicado)
    """
    df = df.copy()

    # Parsear fecha del exact_timestamp
    df["fecha"] = pd.to_datetime(df["exact_timestamp"], errors="coerce").dt.date

    # Fallback: intentar tomar primeros 10 chars como fecha
    mask_null = df["fecha"].isna()
    if mask_null.any():
        df.loc[mask_null, "fecha"] = pd.to_datetime(
            df.loc[mask_null, "exact_timestamp"].str[:10], errors="coerce"
        ).dt.date

    # Ordenar por cuit, fecha, timestamp desc
    df = df.sort_values(
        ["cuit", "fecha", "exact_timestamp"],
        ascending=[True, True, False],
    ).reset_index(drop=True)

    # Row number dentro de (cuit, fecha)
    df["_rn"] = df.groupby(["cuit", "fecha"]).cumcount() + 1
    df["duplicado"] = df["_rn"].apply(lambda x: "No" if x == 1 else "Sí")
    df.drop(columns=["_rn"], inplace=True)

    n_no = (df["duplicado"] == "No").sum()
    n_si = (df["duplicado"] == "Sí").sum()
    print(f"  ✅ {n_no} no duplicados, {n_si} duplicados")
    return df


# ═══════════════════════════════════════════════════════════════
# PASO 3: Verificación en DGR Gestión
# ═══════════════════════════════════════════════════════════════

class DGRTableParser(HTMLParser):
    """Parsea la tabla id='dj' del resultado de DGR."""

    def __init__(self):
        super().__init__()
        self.in_target = False
        self.in_td = False
        self.cell = ""
        self.current_row = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "table" and d.get("id") == "dj":
            self.in_target = True
        elif self.in_target:
            if tag == "td":
                self.in_td = True
                self.cell = ""
            elif tag == "tr":
                self.current_row = []

    def handle_endtag(self, tag):
        if tag == "table" and self.in_target:
            self.in_target = False
        elif self.in_target:
            if tag == "td":
                self.in_td = False
                self.current_row.append(self.cell.strip())
            elif tag == "tr" and self.current_row:
                self.rows.append(self.current_row)

    def handle_data(self, data):
        if self.in_td:
            self.cell += data


def dgr_login(session: requests.Session, username: str, password: str) -> bool:
    """Login en DGR Gestión. Retorna True si fue exitoso."""
    print(f"  [dgr_login] user='{username}' pass_len={len(password) if password else 0}")

    # Acceder al login para obtener cookies
    try:
        r0 = session.get(f"{DGR_BASE}/login.jsp", timeout=30)
        print(f"  [dgr_login] GET login.jsp -> status={r0.status_code} url={r0.url}")
        print(f"  [dgr_login] cookies tras GET: {dict(session.cookies)}")
    except Exception as e:
        print(f"  [dgr_login] EXCEPCION en GET login.jsp: {type(e).__name__}: {e}")
        return False

    # POST de autenticación
    try:
        resp = session.post(
            DGR_LOGIN_URL,
            data={"j_username": username, "j_password": password},
            timeout=30,
            allow_redirects=True,
        )
    except Exception as e:
        print(f"  [dgr_login] EXCEPCION en POST j_security_check: {type(e).__name__}: {e}")
        return False

    print(f"  [dgr_login] POST j_security_check -> status={resp.status_code} final_url={resp.url}")
    print(f"  [dgr_login] historial redirects: {[(h.status_code, h.url) for h in resp.history]}")
    print(f"  [dgr_login] cookies tras POST: {dict(session.cookies)}")
    snippet = (resp.text or "")[:600].replace("\n", " ")
    print(f"  [dgr_login] body[:600]: {snippet}")

    # Si redirige de vuelta al login, falló
    if "login.jsp" in resp.url or "j_security_check" in resp.url:
        print("  [dgr_login] FALLO: la URL final indica vuelta al login")
        return False
    if resp.status_code != 200:
        print("  [dgr_login] FALLO: status_code != 200")
        return False
    print("  [dgr_login] OK")
    return True


def dgr_init_search_form(session: requests.Session) -> bool:
    """
    Carga el formulario de búsqueda por CUIT para inicializar
    la sesión Struts (colecciones de combos, etc.).
    Debe llamarse una vez después del login, antes de buscar.
    """
    resp = session.get(
        DGR_SEARCH_URL, params={"method": "buscarCuitIn"}, timeout=30
    )
    return resp.status_code == 200


def dgr_search_cuit(
    session: requests.Session,
    cuit: str,
    start_date: str,
    end_date: str,
    debug: bool = False,
) -> list[dict]:
    """
    Busca un CUIT en DGR → SIFERE WEB → DDJJ Mensuales → Por CUIT.
    Envía los filtros de período para que DGR devuelva resultados en el rango.
    Retorna lista de dicts con las DDJJ encontradas.
    """
    dt_desde = datetime.strptime(start_date, "%Y-%m-%d")
    dt_hasta = datetime.strptime(end_date, "%Y-%m-%d")

    # Anticipos: retroceder 3 meses para cubrir anticipos de períodos
    # anteriores que se presentan dentro del rango de fechas
    # (ej: anticipo 202512 presentado en enero 2026)
    dt_anticipo_desde = dt_desde - relativedelta(months=3)

    fecha_alta_desde = dt_desde.strftime("%d/%m/%Y")
    fecha_alta_hasta = dt_hasta.strftime("%d/%m/%Y")

    params = {
        "method": "buscarCuit",
        "cuit": cuit,
        "estado": "4",
        "flgTipoPeriodo": "A",
        "anticipoMesDesde": str(dt_anticipo_desde.month).zfill(2),
        "anticipoAnioDesde": str(dt_anticipo_desde.year),
        "anticipoMesHasta": str(dt_hasta.month).zfill(2),
        "anticipoAnioHasta": str(dt_hasta.year),
        "fechaAltaDesde": fecha_alta_desde,
        "fechaAltaHasta": fecha_alta_hasta,
        "fechaPresentacionDesde": fecha_alta_desde,
        "fechaPresentacionHasta": fecha_alta_hasta,
    }

    resp = session.get(DGR_SEARCH_URL, params=params, timeout=30)

    if debug:
        debug_file = f"debug_dgr_{cuit}.html"
        with open(debug_file, "w", encoding="utf-8") as f:
            f.write(resp.text)
        print(f"  🐛 Debug: respuesta DGR guardada en {debug_file}")

    if resp.status_code != 200:
        return []

    parser = DGRTableParser()
    parser.feed(resp.text)

    # Columnas de la tabla dj:
    # Nº DDJJ | Estado | Anticipo | Formulario | Nº Transacción AFIP |
    # Fecha Alta | Fecha Ult Modif | Fecha Presentación AFIP | Resultado AFIP
    cols = [
        "nro_ddjj", "estado", "anticipo", "formulario",
        "nro_transaccion_afip", "fecha_alta", "fecha_ult_modif",
        "fecha_presentacion_afip", "resultado_afip",
    ]

    results = []
    for row in parser.rows:
        if len(row) >= 8:
            record = {}
            for i, col in enumerate(cols):
                record[col] = row[i] if i < len(row) else ""
            results.append(record)

    return results


def verificar_en_dgr(
    df: pd.DataFrame,
    username: str,
    password: str,
    start_date: str,
    end_date: str,
    delay: float = 0.5,
    debug_cuit: str | None = None,
    prev_verif: dict | None = None,
) -> pd.DataFrame:
    """
    Para cada CUIT no duplicado, busca en DGR si tiene (S) en Formulario.
    Agrega columna 'verificada_dgr'.
    prev_verif: dict de (cuit, fecha_str) -> valor verificada_dgr de corridas previas.
    """
    df = df.copy()
    df["verificada_dgr"] = ""

    # Pre-poblar verificaciones previas
    mask = df["duplicado"] == "No"
    if prev_verif:
        for idx in df.index[mask]:
            key = (str(df.at[idx, "cuit"]), str(df.at[idx, "fecha"]))
            if key in prev_verif:
                df.at[idx, "verificada_dgr"] = prev_verif[key]
        carried = (df.loc[mask, "verificada_dgr"] != "").sum()
        pending = mask.sum() - carried
        print(f"  ♻️  Reutilizadas: {carried}, nuevas por verificar: {pending}")

    # Solo verificar los no duplicados que aún no tienen verificación
    mask_need = mask & (df["verificada_dgr"] == "")
    cuits_unicos = df.loc[mask_need, "cuit"].unique()

    if len(cuits_unicos) == 0:
        print("  ✅ No hay CUITs nuevos para verificar en DGR.")
        ok = (df.loc[mask, "verificada_dgr"] == "Sí").sum()
        no = (df.loc[mask, "verificada_dgr"] == "No").sum()
        print(f"  ✅ Verificación DGR (total): {ok} confirmadas, {no} no encontradas")
        return df

    print(f"  🔍 Verificando {len(cuits_unicos)} CUITs en DGR Gestión...")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) COMARB-Verificacion/1.0"
    })

    # Login
    print("  🔑 Iniciando sesión en DGR...")
    if not dgr_login(session, username, password):
        print("  ❌ Error de autenticación en DGR. Verificá usuario/contraseña.")
        df.loc[mask, "verificada_dgr"] = "Error login"
        return df
    print("  ✅ Login exitoso")

    # Inicializar formulario de búsqueda (carga colecciones Struts)
    dgr_init_search_form(session)

    # Cache de resultados por CUIT (un CUIT puede aparecer en varios días)
    cache = {}
    errores = 0

    for i, cuit in enumerate(cuits_unicos, 1):
        if i % 10 == 0 or i == 1:
            print(f"  ⏳ Procesando {i}/{len(cuits_unicos)}...")

        try:
            if cuit not in cache:
                is_debug = debug_cuit and cuit == debug_cuit
                ddjjs = dgr_search_cuit(
                    session, cuit, start_date, end_date, debug=is_debug
                )
                cache[cuit] = ddjjs
                time.sleep(delay)  # No sobrecargar el servidor

            ddjjs = cache[cuit]

            # Verificar por fecha: para cada fila de este CUIT sin verificar,
            # buscar en DGR una DDJJ cuya fecha_presentacion_afip coincida
            # con la fecha del evento GA4 y tenga (S) en formulario.
            rows_cuit = df.index[mask_need & (df["cuit"] == cuit)]
            for idx in rows_cuit:
                fecha_ga4 = df.at[idx, "fecha"]
                resultado = "No"
                for d in ddjjs:
                    fp = d.get("fecha_presentacion_afip", "")
                    try:
                        fecha_dgr = datetime.strptime(fp, "%d/%m/%Y").date()
                    except (ValueError, TypeError):
                        continue
                    if fecha_dgr == fecha_ga4 and "(S)" in d.get("formulario", ""):
                        resultado = "Sí"
                        break
                df.at[idx, "verificada_dgr"] = resultado

        except Exception as e:
            errores += 1
            df.loc[mask_need & (df["cuit"] == cuit), "verificada_dgr"] = "Error"
            if errores <= 3:
                print(f"  ⚠️  Error consultando CUIT {cuit}: {e}")
            elif errores == 4:
                print(f"  ⚠️  Errores sucesivos, se omiten mensajes...")

    ok = (df.loc[mask, "verificada_dgr"] == "Sí").sum()
    no = (df.loc[mask, "verificada_dgr"] == "No").sum()
    err = df.loc[mask, "verificada_dgr"].isin(["Error", "Error login"]).sum()
    print(f"  ✅ Verificación DGR: {ok} confirmadas, {no} no encontradas, {err} errores")

    return df


# ═══════════════════════════════════════════════════════════════
# PASO 4: Generación de reporte HTML
# ═══════════════════════════════════════════════════════════════

def generate_report(df: pd.DataFrame, start_date: str, end_date: str, con_dgr: bool, chart_data: dict | None = None) -> str:
    """Genera reporte HTML con tablas, KPIs y gráficos."""

    generated = datetime.now().strftime("%d/%m/%Y %H:%M")

    # KPIs
    total_registros = len(df)
    total_no_dup = (df["duplicado"] == "No").sum()
    total_dup = (df["duplicado"] == "Sí").sum()
    cuits_unicos = df["cuit"].nunique()

    if con_dgr:
        mask_no = df["duplicado"] == "No"
        dgr_no = (df.loc[mask_no, "verificada_dgr"] == "No").sum()
    else:
        dgr_no = 0

    # Mapeo de nombres de evento legibles
    EVENT_LABELS = {
        "PS_boton_presentar_y_salir": "Presentar y Salir",
        "PS_boton_presentar_y_generar_pago": "Presentar y Generar Pago",
    }

    # ── Tabla con duplicados ──
    def build_table_rows(subset, include_dgr=False):
        rows_html = ""
        for _, r in subset.iterrows():
            dup_class = ' class="dup"' if r["duplicado"] == "Sí" else ""
            dgr_cell = ""
            if include_dgr:
                v = r.get("verificada_dgr", "")
                if v == "Sí":
                    dgr_cell = '<td class="dgr-si">Sí</td>'
                elif v == "No":
                    dgr_cell = '<td class="dgr-no">No</td>'
                elif v:
                    dgr_cell = f'<td class="dgr-err">{v}</td>'
                else:
                    dgr_cell = '<td>—</td>'

            evento_label = EVENT_LABELS.get(r['nombre_evento'], r['nombre_evento'])
            estrellas = r.get('estrellas_valor', '') or ''
            feedback = r.get('texto_feedback', '') or ''

            rows_html += f"""<tr{dup_class}>
                <td class="mono">{r['cuit']}</td>
                <td>{r['exact_timestamp']}</td>
                <td>{int(r['numero_eventos'])}</td>
                <td><code>{evento_label}</code></td>
                <td>{r['region']}</td>
                <td>{r['total']}</td>
                <td>{estrellas}</td>
                <td>{feedback}</td>
                <td>{r['duplicado']}</td>
                {dgr_cell}
            </tr>"""
        return rows_html

    # Ordenar por timestamp descendente (más reciente primero)
    df_sorted = df.sort_values("exact_timestamp", ascending=False)
    table_all = build_table_rows(df_sorted)
    df_no_dup = df_sorted[df_sorted["duplicado"] == "No"].copy()
    table_no_dup = build_table_rows(df_no_dup, include_dgr=con_dgr)


    # ── KPI DGR extra ──
    dgr_kpi_html = ""
    if con_dgr:
        dgr_kpi_html = f"""
        <div class="kpi">
            <div class="label">No encontradas DGR</div>
            <div class="value v6">{dgr_no}</div>
        </div>"""

    # ── Datos para gráficos ──
    if chart_data:
        import json as _json
        chart_fechas_json = _json.dumps(chart_data["fechas"])
        chart_presentadas_no_dup_json = _json.dumps(chart_data.get("presentadas_no_dup", []))
        chart_enviadas_json = _json.dumps(chart_data["enc_enviadas"])
        chart_cerradas_json = _json.dumps(chart_data["enc_cerradas"])

        # Diferencia: (enc_enviadas + enc_cerradas) - presentadas_no_dup
        no_dup = chart_data.get("presentadas_no_dup", [])
        env = chart_data["enc_enviadas"]
        cer = chart_data["enc_cerradas"]
        diferencia = [
            (env[i] + cer[i]) - no_dup[i] if i < len(no_dup) else 0
            for i in range(len(chart_data["fechas"]))
        ]
        chart_diferencia_json = _json.dumps(diferencia)

        # Estrellas: ordenar 1-5, SIN "vacio"
        est = chart_data.get("estrellas", {})
        est_labels = []
        est_values = []
        for k in ["5", "4", "3", "2", "1"]:
            if k in est:
                est_labels.append(k)
                est_values.append(int(est[k]))
        est_labels_json = _json.dumps(est_labels)
        est_values_json = _json.dumps(est_values)

        # Proporción: 5 estrellas vs resto
        cinco = int(est.get("5", 0))
        resto = sum(int(est.get(k, 0)) for k in ["1", "2", "3", "4"])
        prop_cinco_json = _json.dumps(cinco)
        prop_resto_json = _json.dumps(resto)

        # Nube de palabras: contar frecuencia de palabras
        from collections import Counter
        stopwords = {
            "de", "la", "el", "en", "y", "a", "los", "las", "que", "un", "una",
            "es", "por", "con", "para", "se", "del", "al", "lo", "no", "su",
            "como", "más", "pero", "sus", "le", "ya", "o", "fue", "este",
            "ha", "si", "porque", "esta", "son", "entre", "está", "cuando",
            "muy", "sin", "sobre", "ser", "también", "me", "hasta", "hay",
            "donde", "quien", "desde", "todo", "nos", "durante", "todos",
            "uno", "les", "ni", "contra", "otros", "ese", "eso", "ante",
            "ellos", "e", "esto", "mi", "antes", "algunos", "qué", "unos",
            "yo", "otro", "otras", "otra", "él", "tanto", "esa", "estos",
            "mucho", "quienes", "nada", "muchos", "cual", "poco", "ella",
            "bien", "tengo", "tiene", "hacer", "haber", "poder", "ese",
        }
        word_counts = Counter()
        for text in chart_data.get("feedback", []):
            words = re.findall(r'\b[a-záéíóúñü]{3,}\b', text.lower())
            word_counts.update(w for w in words if w not in stopwords)
        top_words = word_counts.most_common(45)
        if top_words:
            max_count = top_words[0][1]
            wc_list = [
                {"text": w, "size": max(14, int(55 * c / max_count)), "count": c}
                for w, c in top_words
            ]
            word_cloud_data = _json.dumps(wc_list)
        else:
            word_cloud_data = "[]"
    else:
        chart_fechas_json = "[]"
        chart_presentadas_no_dup_json = "[]"
        chart_enviadas_json = "[]"
        chart_cerradas_json = "[]"
        chart_diferencia_json = "[]"
        est_labels_json = "[]"
        est_values_json = "[]"
        prop_cinco_json = "0"
        prop_resto_json = "0"
        word_cloud_data = "[]"

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PS Verificación — COMARB</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
    :root {{
        --bg: #0f1117; --surface: #1a1d27; --surface2: #242836;
        --border: #2e3345; --text: #e4e6f0; --text-dim: #8b90a5;
        --accent: #6c8aff; --green: #45d9a8; --amber: #f59e42;
        --red: #ef5678; --purple: #a78bfa; --cyan: #38bdf8;
        --radius: 12px;
    }}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
        font-family: 'DM Sans', sans-serif;
        background: var(--bg); color: var(--text);
        line-height: 1.6; padding: 2rem;
    }}
    .container {{ max-width: 1400px; margin: 0 auto; }}
    header {{
        margin-bottom: 2rem; padding-bottom: 1.5rem;
        border-bottom: 1px solid var(--border);
    }}
    header h1 {{ font-size: 1.5rem; font-weight: 700; color: var(--accent); margin-bottom: .2rem; }}
    header .meta {{ font-size: .8rem; color: var(--text-dim); }}
    .period-filter {{
        display: flex; align-items: center; gap: .6rem;
        margin-top: .8rem; font-size: .85rem; color: var(--text-dim);
        flex-wrap: wrap;
    }}
    .period-filter label {{ font-weight: 600; color: var(--text); }}
    .period-filter input[type="date"] {{
        background: var(--surface); color: var(--text);
        border: 1px solid var(--border); border-radius: 6px;
        padding: .35rem .6rem; font-family: inherit; font-size: .85rem;
        color-scheme: dark;
    }}
    .period-filter input[type="date"]:focus {{
        outline: none; border-color: var(--accent);
    }}
    .period-filter button {{
        background: var(--surface); color: var(--text-dim);
        border: 1px solid var(--border); border-radius: 6px;
        padding: .35rem .65rem; cursor: pointer; font-size: 1rem;
        line-height: 1;
    }}
    .period-filter button:hover {{ color: var(--accent); border-color: var(--accent); }}

    .kpis {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
        gap: 1rem; margin-bottom: 2rem;
    }}
    .kpi {{
        background: var(--surface); border: 1px solid var(--border);
        border-radius: var(--radius); padding: 1.1rem;
    }}
    .kpi .label {{
        font-size: .72rem; text-transform: uppercase;
        letter-spacing: .06em; color: var(--text-dim); margin-bottom: .3rem;
    }}
    .kpi .value {{
        font-size: 1.7rem; font-weight: 700;
        font-family: 'JetBrains Mono', monospace;
    }}
    .v1 {{ color: var(--accent); }} .v2 {{ color: var(--green); }}
    .v3 {{ color: var(--amber); }} .v4 {{ color: var(--red); }}
    .v5 {{ color: var(--green); }} .v6 {{ color: var(--red); }}

    .section {{ margin-bottom: 2rem; }}
    .section h2 {{
        font-size: 1.05rem; font-weight: 600; margin-bottom: 1rem;
        padding-left: .5rem; border-left: 3px solid var(--accent);
    }}
    .card {{
        background: var(--surface); border: 1px solid var(--border);
        border-radius: var(--radius); padding: 1.2rem; overflow-x: auto;
    }}

    table {{ width: 100%; border-collapse: collapse; font-size: .82rem; }}
    th {{
        text-align: left; padding: .55rem .7rem; font-weight: 600;
        color: var(--text-dim); font-size: .72rem; text-transform: uppercase;
        letter-spacing: .04em; border-bottom: 1px solid var(--border);
        position: sticky; top: 0; background: var(--surface);
    }}
    thead tr:first-child th {{
        cursor: pointer; user-select: none; white-space: nowrap;
    }}
    thead tr:first-child th:hover {{ color: var(--accent); }}
    th .sort-arrow {{
        font-size: .65rem; margin-left: .3rem; color: var(--text-dim); opacity: .4;
    }}
    th.sort-active .sort-arrow {{ opacity: 1; color: var(--accent); }}
    td {{
        padding: .5rem .7rem;
        border-bottom: 1px solid var(--border);
    }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: rgba(108,138,255,0.04); }}
    tr.dup td {{ opacity: 0.5; }}
    td.mono {{
        font-family: 'JetBrains Mono', monospace;
        font-size: .78rem; color: var(--accent);
    }}
    code {{
        font-family: 'JetBrains Mono', monospace;
        font-size: .75rem; color: var(--purple);
    }}
    .dgr-si {{ color: var(--green); font-weight: 600; }}
    .dgr-no {{ color: var(--red); font-weight: 600; }}
    .dgr-err {{ color: var(--amber); }}

    .tabs {{
        display: flex; gap: .5rem; margin-bottom: 1rem;
    }}
    .tab {{
        padding: .5rem 1.2rem; border-radius: 8px; cursor: pointer;
        font-size: .85rem; font-weight: 500; border: 1px solid var(--border);
        background: var(--surface); color: var(--text-dim);
        transition: all .2s;
    }}
    .tab.active {{
        background: var(--accent); color: #fff; border-color: var(--accent);
    }}
    .tab-content {{ display: none; }}
    .tab-content.active {{ display: block; }}

    footer {{
        margin-top: 3rem; padding-top: 1rem;
        border-top: 1px solid var(--border);
        font-size: .72rem; color: var(--text-dim); text-align: center;
    }}

    /* Filtros de columna */
    tr.filter-row th {{
        padding: .3rem .4rem; border-bottom: 2px solid var(--border);
    }}
    .col-filter {{
        width: 100%; padding: .35rem .5rem;
        font-family: 'DM Sans', sans-serif; font-size: .75rem;
        background: var(--surface2); color: var(--text);
        border: 1px solid var(--border); border-radius: 6px;
        outline: none; transition: border-color .2s;
    }}
    .col-filter:focus {{
        border-color: var(--accent);
    }}
    .col-filter::placeholder {{
        color: var(--text-dim); opacity: .6;
    }}

    /* Gráficos */
    .charts-grid {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 1.2rem; margin-bottom: 2rem;
    }}
    .chart-card {{
        background: var(--surface); border: 1px solid var(--border);
        border-radius: var(--radius); padding: 1.2rem;
    }}
    .chart-card.full {{ grid-column: 1 / -1; }}
    .chart-card h3 {{
        font-size: .85rem; font-weight: 600; color: var(--text-dim);
        margin-bottom: .8rem; text-transform: uppercase; letter-spacing: .04em;
    }}
    .chart-card canvas {{ width: 100% !important; }}

    /* Tabla con barras horizontales (estilo Looker) */
    .bar-table {{
        width: 100%; border-collapse: collapse; font-size: .8rem;
        font-family: 'DM Sans', sans-serif;
    }}
    .bar-table thead th {{
        text-align: left; padding: .5rem .6rem; font-weight: 600;
        color: var(--accent); font-size: .72rem; text-transform: uppercase;
        letter-spacing: .04em; border-bottom: 2px solid var(--border);
        position: sticky; top: 0; background: var(--surface);
    }}
    .bar-table tbody td {{
        padding: .25rem .6rem; border-bottom: 1px solid rgba(46,51,69,0.5);
        vertical-align: middle; white-space: nowrap;
    }}
    .bar-table tbody tr:hover td {{ background: rgba(108,138,255,0.04); }}
    .bar-table .bar-cell {{
        display: flex; align-items: center; gap: .4rem;
    }}
    .bar-table .bar-cell .bar-val {{
        min-width: 28px; text-align: right;
        font-family: 'JetBrains Mono', monospace; font-size: .75rem;
    }}
    .bar-table .bar-cell .bar {{
        height: 12px; border-radius: 2px; min-width: 0;
    }}
    .bar-table .dif-pos {{ color: var(--text-dim); }}
    .bar-table .dif-neg {{ color: var(--red); }}
    .bar-table-wrap {{
        max-height: 520px; overflow-y: auto;
    }}

    /* Valoraciones + Doughnut + Nube en una fila */
    .val-feedback-grid {{
        display: grid; grid-template-columns: 2fr 1fr 2fr;
        gap: 1.2rem; align-items: center;
    }}
    .val-feedback-grid > div {{ min-height: 0; }}
    .val-feedback-grid .doughnut-wrap {{
        display: flex; justify-content: center; align-items: center;
        max-height: 280px;
    }}
    .val-feedback-grid .doughnut-wrap canvas {{ max-width: 260px; max-height: 260px; }}

    .word-cloud {{
        position: relative;
        width: 100%; height: 380px;
        overflow: hidden;
    }}
    .word-cloud span {{
        cursor: default; transition: opacity .2s;
        font-weight: 500;
    }}
    .word-cloud span:hover {{ opacity: .7; }}
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
</head>
<body>
<div class="container">

<header>
    <h1>📋 Presentación Simplificada — Verificación</h1>
    <div class="meta">
        Propiedad: {PROPERTY_ID} · Generado: {generated}
    </div>
    <div class="period-filter">
        <label>Período:</label>
        <input type="date" id="fecha-desde" value="{start_date}" min="{start_date}" max="{end_date}">
        <span>a</span>
        <input type="date" id="fecha-hasta" value="{end_date}" min="{start_date}" max="{end_date}">
        <button type="button" id="period-reset" title="Restablecer período completo">↺</button>
    </div>
</header>

<div class="kpis">
    <div class="kpi">
        <div class="label">Total registros</div>
        <div class="value v1">{total_registros}</div>
    </div>
    <div class="kpi">
        <div class="label">CUITs únicos</div>
        <div class="value v2">{cuits_unicos}</div>
    </div>
    <div class="kpi">
        <div class="label">No duplicados</div>
        <div class="value v3">{total_no_dup}</div>
    </div>
    <div class="kpi">
        <div class="label">Duplicados</div>
        <div class="value v4">{total_dup}</div>
    </div>
    {dgr_kpi_html}
</div>

<div class="charts-grid">
    <div class="chart-card full">
        <h3>Presentadas (sin duplicados), Encuestas enviadas, cerradas y Diferencia por día</h3>
        <div class="bar-table-wrap">
            <table class="bar-table" id="barTable">
                <thead>
                    <tr>
                        <th>Día</th>
                        <th>Cantidad sin duplicados</th>
                        <th>Cantidad de encuestas enviadas</th>
                        <th>Cantidad de encuestas cerradas</th>
                        <th>Diferencia</th>
                    </tr>
                </thead>
                <tbody id="barTableBody"></tbody>
            </table>
        </div>
    </div>
    <div class="chart-card full">
        <h3>Encuesta: Valoraciones (Estrellas) y Feedback</h3>
        <div class="val-feedback-grid">
            <div><canvas id="chartEstrellas"></canvas></div>
            <div class="doughnut-wrap"><canvas id="chartProporcion"></canvas></div>
            <div class="word-cloud" id="wordCloud"></div>
        </div>
    </div>
</div>

<div class="section">
    <div class="tabs">
        <div class="tab active" onclick="switchTab('no-dup')">Sin duplicados ({total_no_dup})</div>
        <div class="tab" onclick="switchTab('all')">Con duplicados ({total_registros})</div>
    </div>

    <div id="tab-no-dup" class="tab-content active">
        <div class="card">
            <table id="tbl-no-dup">
                <thead>
                    <tr>
                        <th data-col="0">CUIT <span class="sort-arrow">&#x25B2;</span></th>
                        <th data-col="1" class="sort-active" data-dir="desc">Timestamp <span class="sort-arrow">&#x25BC;</span></th>
                        <th data-col="2">Nº Eventos <span class="sort-arrow">&#x25B2;</span></th>
                        <th data-col="3">Evento <span class="sort-arrow">&#x25B2;</span></th>
                        <th data-col="4">Región <span class="sort-arrow">&#x25B2;</span></th>
                        <th data-col="5">Total <span class="sort-arrow">&#x25B2;</span></th>
                        <th data-col="6">Estrellas <span class="sort-arrow">&#x25B2;</span></th>
                        <th data-col="7">Feedback <span class="sort-arrow">&#x25B2;</span></th>
                        <th data-col="8">Duplicado <span class="sort-arrow">&#x25B2;</span></th>
                        {('<th data-col="9">Verificada DGR <span class="sort-arrow">&#x25B2;</span></th>' if con_dgr else '')}
                    </tr>
                    <tr class="filter-row">
                        <th><input class="col-filter" data-col="0" placeholder="Filtrar..."></th>
                        <th><input class="col-filter" data-col="1" placeholder="Filtrar..."></th>
                        <th><input class="col-filter" data-col="2" placeholder="Filtrar..."></th>
                        <th><input class="col-filter" data-col="3" placeholder="Filtrar..."></th>
                        <th><input class="col-filter" data-col="4" placeholder="Filtrar..."></th>
                        <th><input class="col-filter" data-col="5" placeholder="Filtrar..."></th>
                        <th><input class="col-filter" data-col="6" placeholder="Filtrar..."></th>
                        <th><input class="col-filter" data-col="7" placeholder="Filtrar..."></th>
                        <th><input class="col-filter" data-col="8" placeholder="Filtrar..."></th>
                        {'<th><input class="col-filter" data-col="9" placeholder="Filtrar..."></th>' if con_dgr else ''}
                    </tr>
                </thead>
                <tbody>{table_no_dup}</tbody>
            </table>
        </div>
    </div>

    <div id="tab-all" class="tab-content">
        <div class="card">
            <table id="tbl-all">
                <thead>
                    <tr>
                        <th data-col="0">CUIT <span class="sort-arrow">&#x25B2;</span></th>
                        <th data-col="1" class="sort-active" data-dir="desc">Timestamp <span class="sort-arrow">&#x25BC;</span></th>
                        <th data-col="2">Nº Eventos <span class="sort-arrow">&#x25B2;</span></th>
                        <th data-col="3">Evento <span class="sort-arrow">&#x25B2;</span></th>
                        <th data-col="4">Región <span class="sort-arrow">&#x25B2;</span></th>
                        <th data-col="5">Total <span class="sort-arrow">&#x25B2;</span></th>
                        <th data-col="6">Estrellas <span class="sort-arrow">&#x25B2;</span></th>
                        <th data-col="7">Feedback <span class="sort-arrow">&#x25B2;</span></th>
                        <th data-col="8">Duplicado <span class="sort-arrow">&#x25B2;</span></th>
                    </tr>
                    <tr class="filter-row">
                        <th><input class="col-filter" data-col="0" placeholder="Filtrar..."></th>
                        <th><input class="col-filter" data-col="1" placeholder="Filtrar..."></th>
                        <th><input class="col-filter" data-col="2" placeholder="Filtrar..."></th>
                        <th><input class="col-filter" data-col="3" placeholder="Filtrar..."></th>
                        <th><input class="col-filter" data-col="4" placeholder="Filtrar..."></th>
                        <th><input class="col-filter" data-col="5" placeholder="Filtrar..."></th>
                        <th><input class="col-filter" data-col="6" placeholder="Filtrar..."></th>
                        <th><input class="col-filter" data-col="7" placeholder="Filtrar..."></th>
                        <th><input class="col-filter" data-col="8" placeholder="Filtrar..."></th>
                    </tr>
                </thead>
                <tbody>{table_all}</tbody>
            </table>
        </div>
    </div>
</div>

<footer>
    PS Verificación · COMARB · Datos: GA4 Data API + DGR Gestión
</footer>

</div>
<script>
function switchTab(id) {{
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
    document.getElementById('tab-' + id).classList.add('active');
    event.target.classList.add('active');
}}

/* ── Filtrado combinado (columnas + rango de fechas) ── */
function applyFilters(table) {{
    const tbody = table.querySelector('tbody');
    const filters = table.querySelectorAll('.col-filter');
    const desde = document.getElementById('fecha-desde').value;
    const hasta = document.getElementById('fecha-hasta').value;

    tbody.querySelectorAll('tr').forEach(row => {{
        const cells = row.querySelectorAll('td');
        let show = true;

        /* Filtros por columna */
        filters.forEach(f => {{
            const col = parseInt(f.dataset.col);
            const val = f.value.toLowerCase();
            if (val && cells[col]) {{
                const text = cells[col].textContent.toLowerCase();
                if (!text.includes(val)) show = false;
            }}
        }});

        /* Filtro por rango de fechas (col 1 = Timestamp, primeros 10 chars = YYYY-MM-DD) */
        if (show && cells[1]) {{
            const fechaRow = cells[1].textContent.trim().slice(0, 10);
            if (desde && fechaRow < desde) show = false;
            if (hasta && fechaRow > hasta) show = false;
        }}

        row.style.display = show ? '' : 'none';
    }});
}}

function applyFiltersAll() {{
    document.querySelectorAll('table').forEach(t => {{
        if (t.querySelector('.col-filter')) applyFilters(t);
    }});
}}

document.querySelectorAll('.col-filter').forEach(input => {{
    input.addEventListener('input', function() {{
        applyFilters(this.closest('table'));
    }});
}});

/* Inputs del rango de fechas */
const fechaDesdeEl = document.getElementById('fecha-desde');
const fechaHastaEl = document.getElementById('fecha-hasta');
const periodResetEl = document.getElementById('period-reset');
const defaultDesde = fechaDesdeEl.value;
const defaultHasta = fechaHastaEl.value;

fechaDesdeEl.addEventListener('change', applyFiltersAll);
fechaHastaEl.addEventListener('change', applyFiltersAll);
periodResetEl.addEventListener('click', () => {{
    fechaDesdeEl.value = defaultDesde;
    fechaHastaEl.value = defaultHasta;
    applyFiltersAll();
}});

/* Ordenamiento por columna */
document.querySelectorAll('thead tr:first-child th[data-col]').forEach(th => {{
    th.addEventListener('click', function() {{
        const table = this.closest('table');
        const tbody = table.querySelector('tbody');
        const col = parseInt(this.dataset.col);
        const headerRow = this.parentElement;

        /* Determinar dirección */
        const wasActive = this.classList.contains('sort-active');
        const oldDir = this.dataset.dir || 'asc';
        const newDir = wasActive ? (oldDir === 'asc' ? 'desc' : 'asc') : 'asc';

        /* Resetear todos los headers de esta tabla */
        headerRow.querySelectorAll('th[data-col]').forEach(h => {{
            h.classList.remove('sort-active');
            h.dataset.dir = 'asc';
            const arrow = h.querySelector('.sort-arrow');
            if (arrow) arrow.innerHTML = '&#x25B2;';
        }});

        /* Activar el actual */
        this.classList.add('sort-active');
        this.dataset.dir = newDir;
        const arrow = this.querySelector('.sort-arrow');
        if (arrow) arrow.innerHTML = newDir === 'asc' ? '&#x25B2;' : '&#x25BC;';

        /* Ordenar filas */
        const rows = Array.from(tbody.querySelectorAll('tr'));
        rows.sort((a, b) => {{
            const aText = a.querySelectorAll('td')[col]?.textContent.trim() || '';
            const bText = b.querySelectorAll('td')[col]?.textContent.trim() || '';
            const aNum = parseFloat(aText);
            const bNum = parseFloat(bText);
            let cmp;
            if (!isNaN(aNum) && !isNaN(bNum)) {{
                cmp = aNum - bNum;
            }} else {{
                cmp = aText.localeCompare(bText, 'es');
            }}
            return newDir === 'asc' ? cmp : -cmp;
        }});
        rows.forEach(r => tbody.appendChild(r));
    }});
}});

/* ── Gráficos ── */
Chart.defaults.color = '#8b90a5';
Chart.defaults.font.family = "'DM Sans', sans-serif";

/* Tabla con barras horizontales (estilo Looker) */
const fechas = {chart_fechas_json};
const pres = {chart_presentadas_no_dup_json};
const env = {chart_enviadas_json};
const cerr = {chart_cerradas_json};
const dif = {chart_diferencia_json};

if (fechas.length > 0) {{
    const maxVal = Math.max(...pres, ...env, ...cerr, 1);
    const tbody = document.getElementById('barTableBody');

    /* Recorrer en orden descendente (fecha más reciente primero) */
    for (let i = fechas.length - 1; i >= 0; i--) {{
        const tr = document.createElement('tr');
        const barPct = v => Math.max(0, (v / maxVal) * 100);
        const barHtml = (val, color) =>
            '<div class="bar-cell"><span class="bar-val">' + val +
            '</span><span class="bar" style="width:' + barPct(val) +
            '%;background:' + color + '"></span></div>';
        const difVal = dif[i];
        const difClass = difVal < 0 ? 'dif-neg' : 'dif-pos';

        tr.innerHTML =
            '<td style="font-family:JetBrains Mono,monospace;font-size:.75rem">' + fechas[i] + '</td>' +
            '<td>' + barHtml(pres[i], '#6c8aff') + '</td>' +
            '<td>' + barHtml(env[i], '#45d9a8') + '</td>' +
            '<td>' + barHtml(cerr[i], '#f59e42') + '</td>' +
            '<td class="' + difClass + '" style="text-align:right;font-family:JetBrains Mono,monospace;font-size:.75rem">' + difVal + '</td>';
        tbody.appendChild(tr);
    }}
}}

/* Gráfico de barras: Valoraciones (sin vacio) */
const estLabels = {est_labels_json};
if (estLabels.length > 0) {{
    const estColors = estLabels.map(l => ({{
        '1': '#ef5678', '2': '#f59e42', '3': '#f59e42',
        '4': '#45d9a8', '5': '#6c8aff',
    }})[l] || '#8b90a5');

    new Chart(document.getElementById('chartEstrellas'), {{
        type: 'bar',
        data: {{
            labels: estLabels.map(l => l + ' ★'),
            datasets: [{{
                data: {est_values_json},
                backgroundColor: estColors.map(c => c + 'cc'),
                borderColor: estColors,
                borderWidth: 1,
                borderRadius: 6,
            }}],
        }},
        options: {{
            responsive: true,
            plugins: {{ legend: {{ display: false }} }},
            scales: {{
                x: {{ grid: {{ display: false }} }},
                y: {{ grid: {{ color: '#2e3345' }}, beginAtZero: true }},
            }},
        }},
    }});
}}

/* Gráfico de proporción: 5★ vs Resto */
const cinco = {prop_cinco_json};
const resto = {prop_resto_json};
if (cinco + resto > 0) {{
    new Chart(document.getElementById('chartProporcion'), {{
        type: 'doughnut',
        data: {{
            labels: ['5 ★', 'Resto (1-4 ★)'],
            datasets: [{{
                data: [cinco, resto],
                backgroundColor: ['#6c8affcc', '#f59e42cc'],
                borderColor: ['#6c8aff', '#f59e42'],
                borderWidth: 2,
            }}],
        }},
        options: {{
            responsive: true,
            cutout: '55%',
            plugins: {{
                legend: {{ position: 'bottom', labels: {{ usePointStyle: true, pointStyle: 'circle', padding: 16 }} }},
                tooltip: {{
                    callbacks: {{
                        label: function(ctx) {{
                            const total = cinco + resto;
                            const pct = ((ctx.raw / total) * 100).toFixed(1);
                            return ctx.label + ': ' + ctx.raw + ' (' + pct + '%)';
                        }}
                    }}
                }},
            }},
        }},
    }});
}}

/* Nube de palabras — layout espiral compacto */
const wordData = {word_cloud_data};
const cloudEl = document.getElementById('wordCloud');
if (wordData.length > 0) {{
    const colors = ['#6c8aff','#45d9a8','#a78bfa','#38bdf8','#f59e42','#ef5678','#e4e6f0'];
    const W = cloudEl.clientWidth || 1200;
    const H = 380;
    const placed = [];

    /* Medir tamaño de texto con canvas offscreen */
    const measureCanvas = document.createElement('canvas').getContext('2d');
    function measure(text, size) {{
        measureCanvas.font = '500 ' + size + 'px DM Sans, sans-serif';
        const m = measureCanvas.measureText(text);
        return {{ w: m.width + 6, h: size * 1.15 }};
    }}

    function overlaps(x, y, w, h) {{
        for (const p of placed) {{
            if (!(x + w < p.x || x > p.x + p.w || y + h < p.y || y > p.y + p.h)) return true;
        }}
        return false;
    }}

    /* Mezclar para variedad visual */
    const shuffled = [...wordData].sort(() => Math.random() - 0.5);
    const cx = W / 2, cy = H / 2;

    shuffled.forEach((w, i) => {{
        const dim = measure(w.text, w.size);
        /* Intentar en espiral desde el centro */
        let ok = false;
        for (let r = 0; r < Math.max(W, H) && !ok; r += 3) {{
            for (let a = 0; a < 6.28 && !ok; a += 0.3) {{
                const x = cx + r * Math.cos(a) - dim.w / 2;
                const y = cy + r * Math.sin(a) - dim.h / 2;
                if (x >= 0 && x + dim.w <= W && y >= 0 && y + dim.h <= H && !overlaps(x, y, dim.w, dim.h)) {{
                    const span = document.createElement('span');
                    span.textContent = w.text;
                    span.style.cssText = 'position:absolute;left:' + x + 'px;top:' + y + 'px;font-size:' + w.size + 'px;color:' + colors[i % colors.length] + ';font-weight:500;line-height:1.15;white-space:nowrap;cursor:default';
                    span.title = w.text + ': ' + w.count;
                    cloudEl.appendChild(span);
                    placed.push({{ x: x, y: y, w: dim.w, h: dim.h }});
                    ok = true;
                }}
            }}
        }}
    }});
}} else {{
    cloudEl.innerHTML = '<span style="color:#8b90a5;font-size:.85rem;position:relative">Sin datos de feedback</span>';
}}
</script>
</body>
</html>"""
    return html


def load_previous_verifications(csv_path: str) -> dict:
    """Carga verificaciones DGR previas desde un CSV existente.
    Retorna dict de (cuit_str, fecha_str) -> verificada_dgr."""
    p = Path(csv_path)
    if not p.exists():
        return {}

    df_prev = pd.read_csv(csv_path, encoding="utf-8-sig")
    if "verificada_dgr" not in df_prev.columns:
        return {}

    df_prev["verificada_dgr"] = df_prev["verificada_dgr"].fillna("")
    # No reutilizar filas en estado de error: deben reverificarse en la próxima corrida
    error_states = {"", "Error", "Error login"}
    mask = (df_prev["duplicado"] == "No") & (~df_prev["verificada_dgr"].isin(error_states))
    result = {}
    for _, row in df_prev[mask].iterrows():
        key = (str(row["cuit"]), str(row["fecha"]))
        result[key] = row["verificada_dgr"]

    return result


def _build_chart_data_from_df(df: pd.DataFrame) -> dict:
    """Reconstruye chart_data desde un DataFrame (cargado de CSV).
    No tiene datos de encuestas cerradas ya que no están en el CSV."""
    from collections import Counter

    df["_fecha_ts"] = pd.to_datetime(df["exact_timestamp"], errors="coerce").dt.strftime("%Y-%m-%d")

    # Presentadas sin duplicados por día
    df_no = df[df["duplicado"] == "No"]
    no_dup_por_dia = df_no.groupby("_fecha_ts")["numero_eventos"].sum().to_dict()

    # Todas las fechas
    all_dates = sorted(df["_fecha_ts"].dropna().unique())

    # Estrellas
    est_vals = df["estrellas_valor"].replace("", pd.NA).dropna() if "estrellas_valor" in df.columns else pd.Series()
    estrellas_dist = est_vals.value_counts().to_dict() if not est_vals.empty else {}

    # Feedback
    feedback = []
    if "texto_feedback" in df.columns:
        fb = df["texto_feedback"].replace("", pd.NA).dropna().tolist()
        feedback = [t for t in fb if t and t != "(not set)"]

    df.drop(columns=["_fecha_ts"], inplace=True)

    return {
        "fechas": all_dates,
        "presentadas": [0] * len(all_dates),
        "presentadas_no_dup": [int(no_dup_por_dia.get(d, 0)) for d in all_dates],
        "enc_enviadas": [0] * len(all_dates),
        "enc_cerradas": [0] * len(all_dates),
        "estrellas": estrellas_dist,
        "feedback": feedback,
    }


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="PS Verificación — Cruce GA4 + DGR Gestión"
    )
    parser.add_argument(
        "-c", "--credentials", default=None,
        help="Ruta al JSON de Service Account de GA4",
    )
    parser.add_argument(
        "--desde-csv", metavar="CSV_PATH", default=None,
        help="Regenerar reporte HTML desde un CSV existente (no consulta GA4 ni DGR)",
    )
    parser.add_argument(
        "-u", "--usuario-dgr", default=None,
        help="Usuario de DGR Gestión",
    )
    parser.add_argument(
        "-p", "--password-dgr", default=None,
        help="Contraseña de DGR Gestión",
    )
    parser.add_argument(
        "--desde", default="2026-01-01",
        help="Fecha inicio YYYY-MM-DD (default: 2026-01-01)",
    )
    parser.add_argument(
        "--hasta", default="2026-03-31",
        help="Fecha fin YYYY-MM-DD (default: 2026-03-31)",
    )
    parser.add_argument(
        "--solo-ga4", action="store_true",
        help="Solo extraer y deduplicar, sin verificar en DGR",
    )
    parser.add_argument(
        "-o", "--output", default="ps_verificacion.html",
        help="Archivo HTML de salida (default: ps_verificacion.html)",
    )
    parser.add_argument(
        "--delay", type=float, default=0.5,
        help="Segundos entre consultas a DGR (default: 0.5)",
    )
    parser.add_argument(
        "--debug", metavar="CUIT", default=None,
        help="Guardar respuesta HTML cruda de DGR para un CUIT de prueba",
    )
    parser.add_argument(
        "--incremental", action="store_true",
        help="Solo verificar CUITs nuevos en DGR, reutilizando verificaciones previas del CSV",
    )

    args = parser.parse_args()

    # ── Modo desde-csv: regenerar solo HTML ──
    if args.desde_csv:
        print(f"\n{'═' * 60}")
        print(f"  PS Verificación — Regenerar HTML desde CSV")
        print(f"  CSV: {args.desde_csv}")
        print(f"{'═' * 60}\n")

        df = pd.read_csv(args.desde_csv, encoding="utf-8-sig")
        df["numero_eventos"] = pd.to_numeric(df["numero_eventos"], errors="coerce")
        for col in ["estrellas_valor", "texto_feedback"]:
            if col in df.columns:
                df[col] = df[col].fillna("")

        con_dgr = "verificada_dgr" in df.columns

        # Cargar chart_data desde JSON si existe (tiene series de GA4)
        chart_json_path = args.desde_csv.replace(".csv", "_charts.json")
        if Path(chart_json_path).exists():
            with open(chart_json_path, "r", encoding="utf-8") as f:
                chart_data = json.load(f)
            print(f"  📊 Datos de gráficos cargados desde {chart_json_path}")
        else:
            chart_data = _build_chart_data_from_df(df)

        print(f"📝 Generando reporte → {args.output}")
        html = generate_report(df, args.desde, args.hasta, con_dgr, chart_data)

        with open(args.output, "w", encoding="utf-8") as f:
            f.write(html)

        print(f"\n{'═' * 60}")
        print(f"  ✨ Listo!")
        print(f"  📊 Reporte: {args.output}")
        print(f"{'═' * 60}\n")
        return

    # Validar credenciales GA4
    if not args.credentials:
        print("❌ Se requiere -c CREDENCIALES (o usar --desde-csv)")
        sys.exit(1)

    # Validar que si no es solo-ga4, tenga credenciales DGR
    con_dgr = not args.solo_ga4
    if con_dgr and (not args.usuario_dgr or not args.password_dgr):
        print("❌ Para verificar en DGR necesitás -u USUARIO -p CONTRASEÑA")
        print("   O usá --solo-ga4 para omitir la verificación DGR.")
        sys.exit(1)

    print(f"\n{'═' * 60}")
    print(f"  PS Verificación — COMARB")
    print(f"  Período: {args.desde} → {args.hasta}")
    print(f"  DGR: {'Sí' if con_dgr else 'No (solo GA4)'}")
    print(f"{'═' * 60}\n")

    # ── Paso 1: GA4 ──
    print("📡 PASO 1: Extracción de GA4")
    result = extract_ga4_data(args.credentials, args.desde, args.hasta)
    if isinstance(result, tuple):
        df, chart_data = result
    else:
        df, chart_data = result, {}
    if df.empty:
        print("\n❌ Sin datos. Verificá el rango de fechas y los permisos.")
        sys.exit(1)

    # ── Paso 2: Deduplicar ──
    print("\n🔄 PASO 2: Deduplicación")
    df = deduplicate(df)

    # Completar presentadas sin duplicados por día para gráficos
    if chart_data:
        df_no = df[df["duplicado"] == "No"].copy()
        df_no["_fecha_ts"] = pd.to_datetime(df_no["exact_timestamp"], errors="coerce").dt.strftime("%Y-%m-%d")
        no_dup_por_dia = df_no.groupby("_fecha_ts")["numero_eventos"].sum().to_dict()
        chart_data["presentadas_no_dup"] = [
            int(no_dup_por_dia.get(d, 0)) for d in chart_data["fechas"]
        ]

    # ── Paso 3: DGR ──
    if con_dgr:
        print("\n🏛️  PASO 3: Verificación en DGR Gestión")
        prev_verif = {}
        if args.incremental:
            csv_path = args.output.replace(".html", ".csv")
            prev_verif = load_previous_verifications(csv_path)
        df = verificar_en_dgr(
            df, args.usuario_dgr, args.password_dgr,
            args.desde, args.hasta, args.delay, args.debug,
            prev_verif=prev_verif if prev_verif else None,
        )
    else:
        print("\n⏭️  PASO 3: Omitido (--solo-ga4)")

    # ── Paso 4: Reporte ──
    print(f"\n📝 PASO 4: Generando reporte → {args.output}")
    html = generate_report(df, args.desde, args.hasta, con_dgr, chart_data)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)

    # Guardar CSV y chart_data JSON
    csv_path = args.output.replace(".html", ".csv")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    chart_json_path = args.output.replace(".html", "_charts.json")
    if chart_data:
        with open(chart_json_path, "w", encoding="utf-8") as f:
            json.dump(chart_data, f, ensure_ascii=False)

    print(f"\n{'═' * 60}")
    print(f"  ✨ Listo!")
    print(f"  📊 Reporte: {args.output}")
    print(f"  📄 CSV:     {csv_path}")
    print(f"{'═' * 60}\n")


if __name__ == "__main__":
    main()
