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
from datetime import datetime, timedelta, timezone
from dateutil.relativedelta import relativedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

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
GA4_EVENT_ERROR = "PS_error_validacion_dj"
GA4_HOSTNAME = "servicios.comarb.gob.ar"

DGR_BASE = "https://dgrgw.comarb.gob.ar/dgr"
DGR_LOGIN_URL = f"{DGR_BASE}/j_security_check"
DGR_SEARCH_URL = f"{DGR_BASE}/sfrwDdjj.do"
DGR_PADRON_URL = f"{DGR_BASE}/pwContribBlockChain.do"

# TTL del padrón ARCA/BC en días: los contribuyentes pueden modificar sus
# jurisdicciones una vez al mes, así que revalidamos cada 30 días.
TTL_PADRON_DIAS = 30


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
            Dimension(name="customEvent:texto_del_error"),
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

    dim_names = ["cuit", "exact_timestamp", "nombre_evento", "region", "total", "texto_del_error"]
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
        return df, pd.DataFrame(), {}

    df["numero_eventos"] = pd.to_numeric(df["numero_eventos"], errors="coerce")

    # Limpiar (not set) en texto_del_error
    if "texto_del_error" in df.columns:
        df["texto_del_error"] = df["texto_del_error"].replace("(not set)", "")

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

    # ── Query 4: Errores de validación (PS_error_validacion_dj) ──
    print("  📡 Consultando GA4 (errores de validación)...")

    error_filter = FilterExpression(
        and_group=FilterExpressionList(
            expressions=[
                hostname_filter,
                FilterExpression(
                    filter=Filter(
                        field_name="eventName",
                        string_filter=Filter.StringFilter(
                            value=GA4_EVENT_ERROR,
                            match_type=Filter.StringFilter.MatchType.EXACT,
                        ),
                    )
                ),
            ]
        )
    )

    err_request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[
            Dimension(name="customEvent:CUIT"),
            Dimension(name="customEvent:exact_timestamp"),
            Dimension(name="region"),
            Dimension(name="customEvent:Total"),
            Dimension(name="customEvent:texto_del_error"),
        ],
        metrics=[Metric(name="eventCount")],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimension_filter=error_filter,
        limit=10000,
    )

    err_response = client.run_report(err_request)

    err_dim_names = ["cuit", "exact_timestamp", "region", "total", "texto_del_error"]
    err_rows = []
    for row in err_response.rows:
        record = {}
        for i, dv in enumerate(row.dimension_values):
            record[err_dim_names[i]] = dv.value
        record["numero_eventos"] = int(row.metric_values[0].value)
        err_rows.append(record)

    df_err = pd.DataFrame(err_rows)
    if not df_err.empty:
        # Limpiar (not set) en strings
        for col in ["region", "total", "texto_del_error"]:
            df_err[col] = df_err[col].replace("(not set)", "")
        df_err = df_err[~df_err["cuit"].isin(["(not set)", ""])].copy()
        df_err = df_err[df_err["cuit"].str.strip() != ""].copy()
        print(f"  ✅ {len(df_err)} eventos de error de validación extraídos")
    else:
        print("  ⚠️  No se encontraron errores de validación en el período.")

    # Errores por día (para chart_data)
    errores_por_dia = {}
    if not df_err.empty:
        df_err_tmp = df_err.copy()
        df_err_tmp["_fecha_ts"] = pd.to_datetime(df_err_tmp["exact_timestamp"], errors="coerce").dt.strftime("%Y-%m-%d")
        errores_por_dia = df_err_tmp.groupby("_fecha_ts")["numero_eventos"].sum().to_dict()

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
        + list(errores_por_dia.keys())
    ))

    chart_data = {
        "fechas": all_dates,
        "presentadas": [int(presentadas_por_dia.get(d, 0)) for d in all_dates],
        "enc_enviadas": [int(enviadas_por_dia.get(d, 0)) for d in all_dates],
        "enc_cerradas": [int(cerrar_por_dia.get(d, 0)) for d in all_dates],
        "errores_por_dia": [int(errores_por_dia.get(d, 0)) for d in all_dates],
        "presentadas_no_dup": [],  # se llena después de deduplicar en main
        "estrellas": estrellas_dist,
        "feedback": feedback_textos,
    }

    print(f"  ✅ Datos de gráficos: {len(all_dates)} días, {len(feedback_textos)} textos de feedback")

    return df, df_err, chart_data


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


class DGRJurParser(HTMLParser):
    """Extrae los códigos de jurisdicción (prefijo antes del '-' en la 3ra
    columna, ej '910-JUJUY' → '910') del <tbody> del <div id='jur'> del
    Padrón Web (ARCA/BC).

    Ignora filas con menos de 6 <td> (filas 'NO TIENE ...' con colspan).
    Sólo procesa el primer <tbody> dentro de #jur para no mezclar con tabs
    posteriores (rel, documento, telemail, etc.).
    """

    def __init__(self):
        super().__init__()
        self.in_jur = False       # Dentro del <div id="jur">
        self.jur_depth = 0        # Profundidad de <div> anidados dentro de #jur
        self.in_tbody = False
        self.tbody_seen = False   # Ya consumimos el primer tbody de #jur
        self.in_tr = False
        self.td_index = 0         # Índice de <td> en la fila actual (1..6)
        self.in_td = False
        self.cell_buf = ""
        self.current_code = ""    # Código extraído de la 3ra celda
        self.codes: list[str] = []

    @property
    def count(self) -> int:
        """Compat: cantidad de jurisdicciones (tests/uso interno)."""
        return len(self.codes)

    @property
    def codes_str(self) -> str:
        """Códigos de jurisdicción separados por coma, ej '910, 917'."""
        return ", ".join(self.codes)

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "div":
            if self.in_jur:
                self.jur_depth += 1
            elif d.get("id") == "jur":
                self.in_jur = True
                self.jur_depth = 1
        elif self.in_jur and not self.tbody_seen:
            if tag == "tbody":
                self.in_tbody = True
            elif tag == "tr" and self.in_tbody:
                self.in_tr = True
                self.td_index = 0
                self.current_code = ""
            elif tag == "td" and self.in_tr:
                self.td_index += 1
                if self.td_index == 3:
                    self.in_td = True
                    self.cell_buf = ""

    def handle_endtag(self, tag):
        if tag == "div" and self.in_jur:
            self.jur_depth -= 1
            if self.jur_depth <= 0:
                self.in_jur = False
                self.in_tbody = False
        elif self.in_jur and not self.tbody_seen:
            if tag == "tbody" and self.in_tbody:
                self.in_tbody = False
                self.tbody_seen = True
            elif tag == "tr" and self.in_tr:
                # Aceptar fila si tuvo >= 6 <td> y tiene código válido
                if self.td_index >= 6 and self.current_code:
                    self.codes.append(self.current_code)
                self.in_tr = False
                self.td_index = 0
                self.current_code = ""
            elif tag == "td" and self.in_td:
                # Cierre de la 3ra <td>: extraer código (prefijo antes del '-')
                txt = self.cell_buf.strip()
                code = txt.split("-", 1)[0].strip() if "-" in txt else txt
                # Sólo aceptar códigos puramente numéricos (evita filas basura)
                if code.isdigit():
                    self.current_code = code
                self.in_td = False
                self.cell_buf = ""

    def handle_data(self, data):
        if self.in_td:
            self.cell_buf += data


def dgr_login(session: requests.Session, username: str, password: str) -> bool:
    """Login en DGR Gestión. Retorna True si fue exitoso."""
    print(f"  [dgr_login] user='{username}' pass_len={len(password) if password else 0}")

    # Acceder al login para obtener cookies y la URL real del form (con jsessionid)
    try:
        r0 = session.get(f"{DGR_BASE}/login.jsp", timeout=30)
        print(f"  [dgr_login] GET login.jsp -> status={r0.status_code} url={r0.url}")
        print(f"  [dgr_login] cookies tras GET: {dict(session.cookies)}")
    except Exception as e:
        print(f"  [dgr_login] EXCEPCION en GET login.jsp: {type(e).__name__}: {e}")
        return False

    # Tomcat usa URL rewriting: el form action incluye ';jsessionid=...'.
    # Si posteamos a /j_security_check sin ese sufijo, falla la auth.
    m = re.search(
        r'<form[^>]*action="([^"]*j_security_check[^"]*)"',
        r0.text,
        re.IGNORECASE,
    )
    if m:
        action = m.group(1)
        post_url = action if action.startswith("http") else f"https://dgrgw.comarb.gob.ar{action}"
    else:
        post_url = DGR_LOGIN_URL
    print(f"  [dgr_login] form action -> {post_url}")

    # POST de autenticación
    try:
        resp = session.post(
            post_url,
            data={
                "j_username": username,
                "j_password": password,
                "login": "Entrar",
            },
            headers={
                "Referer": f"{DGR_BASE}/login.jsp",
                "Origin": "https://dgrgw.comarb.gob.ar",
                "Content-Type": "application/x-www-form-urlencoded",
            },
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


def dgr_init_padron_web(session: requests.Session) -> bool:
    """
    Carga el formulario de Padrón Web (ARCA/BC) para inicializar
    la sesión Struts antes de buscar.
    """
    resp = session.get(
        DGR_PADRON_URL, params={"method": "buscarInBc"}, timeout=30
    )
    return resp.status_code == 200


def dgr_jurisdicciones_cuit(
    session: requests.Session,
    cuit: str,
    debug: bool = False,
) -> str:
    """
    Obtiene las jurisdicciones asociadas a un CUIT según el Padrón Web (ARCA/BC).
    Retorna un string con los códigos separados por coma (ej: '910, 917'),
    '' si no hay jurisdicciones, o 'Error' si la consulta falla.
    """
    try:
        resp = session.get(
            DGR_PADRON_URL,
            params={"method": "buscar", "cuit": cuit},
            timeout=30,
        )
    except Exception:
        return "Error"

    if debug:
        debug_file = f"debug_padron_{cuit}.html"
        try:
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(resp.text)
            print(f"  🐛 Debug padrón: respuesta guardada en {debug_file}")
        except Exception:
            pass

    if resp.status_code != 200:
        return "Error"

    parser = DGRJurParser()
    try:
        parser.feed(resp.text)
    except Exception:
        return "Error"
    return parser.codes_str


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
    Para cada CUIT no duplicado, busca en DGR si tiene (S) en Formulario
    y consulta el Padrón Web (ARCA/BC) para obtener las jurisdicciones
    asociadas. Agrega columnas 'verificada_dgr', 'jurisdicciones' y
    'jurisdicciones_check' (fecha YYYY-MM-DD de la última consulta al padrón).

    El padrón se refresca si la última consulta tiene más de TTL_PADRON_DIAS
    días, dado que los contribuyentes pueden modificar sus jurisdicciones
    una vez al mes.

    prev_verif: dict de (cuit, fecha_str) -> {'verificada_dgr': ...,
                'jurisdicciones': ..., 'jurisdicciones_check': ...}
                (Acepta también el formato viejo (valor str = verificada_dgr) por
                compatibilidad hacia atrás.)
    """
    df = df.copy()
    df["verificada_dgr"] = ""
    df["jurisdicciones"] = ""
    df["jurisdicciones_check"] = ""

    today_date = datetime.now().date()
    today_str = today_date.strftime("%Y-%m-%d")
    ttl_cutoff = today_date - timedelta(days=TTL_PADRON_DIAS)

    # Pre-poblar verificaciones previas
    mask = df["duplicado"] == "No"
    if prev_verif:
        for idx in df.index[mask]:
            key = (str(df.at[idx, "cuit"]), str(df.at[idx, "fecha"]))
            if key in prev_verif:
                val = prev_verif[key]
                if isinstance(val, dict):
                    df.at[idx, "verificada_dgr"] = val.get("verificada_dgr", "")
                    df.at[idx, "jurisdicciones"] = val.get("jurisdicciones", "")
                    df.at[idx, "jurisdicciones_check"] = val.get("jurisdicciones_check", "")
                else:
                    # Formato viejo: sólo verificada_dgr
                    df.at[idx, "verificada_dgr"] = val
        carried = (df.loc[mask, "verificada_dgr"] != "").sum()
        pending = mask.sum() - carried
        print(f"  ♻️  Reutilizadas: {carried}, nuevas por verificar: {pending}")

    # Filas que requieren consulta DDJJ (verificada_dgr vacío)
    mask_need_ddjj = mask & (df["verificada_dgr"] == "")
    cuits_need_ddjj = df.loc[mask_need_ddjj, "cuit"].unique()

    # Filas que requieren consulta Padrón: jur vacío/en error, O check
    # faltante, O check con más de TTL_PADRON_DIAS días de antigüedad.
    jur_series = df["jurisdicciones"].astype(str)
    # Usamos Timestamps (no date) para poder comparar contra la Series en pandas 3.x
    check_series = pd.to_datetime(df["jurisdicciones_check"], errors="coerce")
    ttl_cutoff_ts = pd.Timestamp(ttl_cutoff)
    jur_stale = jur_series.isin(["", "Error", "Error login"]) | check_series.isna() | (check_series < ttl_cutoff_ts)
    mask_need_jur = mask & jur_stale
    cuits_need_jur = df.loc[mask_need_jur, "cuit"].unique()

    refrescos = int(
        (mask & ~jur_series.isin(["", "Error", "Error login"]) & check_series.notna() & (check_series < ttl_cutoff_ts)).sum()
    )
    if refrescos > 0:
        print(f"  🔄 Padrón: {refrescos} filas con check > {TTL_PADRON_DIAS} días → se refrescan")

    if len(cuits_need_ddjj) == 0 and len(cuits_need_jur) == 0:
        print("  ✅ No hay CUITs nuevos para verificar en DGR.")
        ok = (df.loc[mask, "verificada_dgr"] == "Sí").sum()
        no = (df.loc[mask, "verificada_dgr"] == "No").sum()
        print(f"  ✅ Verificación DGR (total): {ok} confirmadas, {no} no encontradas")
        return df

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) COMARB-Verificacion/1.0"
    })

    # Login (una sola vez, sirve para ambos pases)
    print("  🔑 Iniciando sesión en DGR...")
    if not dgr_login(session, username, password):
        print("  ❌ Error de autenticación en DGR. Verificá usuario/contraseña.")
        # Sólo marcar como Error login las filas que no tenían valor válido previo
        df.loc[mask_need_ddjj, "verificada_dgr"] = "Error login"
        df.loc[mask_need_jur, "jurisdicciones"] = "Error login"
        return df
    print("  ✅ Login exitoso")

    # ═════════════════════════════════════════════════════════════
    # PASE 1: DDJJ (SIFERE WEB → sfrwDdjj.do)
    # ═════════════════════════════════════════════════════════════
    if len(cuits_need_ddjj) > 0:
        print(f"  🔍 [DDJJ] Verificando {len(cuits_need_ddjj)} CUITs en SIFERE WEB...")
        dgr_init_search_form(session)
        errores = 0

        for i, cuit in enumerate(cuits_need_ddjj, 1):
            if i % 10 == 0 or i == 1:
                print(f"  ⏳ [DDJJ] Procesando {i}/{len(cuits_need_ddjj)}...")

            try:
                is_debug = debug_cuit and cuit == debug_cuit
                ddjjs = dgr_search_cuit(
                    session, cuit, start_date, end_date, debug=is_debug
                )
                time.sleep(delay)

                # Para cada fila de este CUIT: match por fecha + (S) en formulario
                rows_cuit = df.index[mask_need_ddjj & (df["cuit"] == cuit)]
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
                # Sólo sobreescribir filas todavía vacías (defensive)
                vacias = mask_need_ddjj & (df["cuit"] == cuit) & (df["verificada_dgr"] == "")
                df.loc[vacias, "verificada_dgr"] = "Error"
                if errores <= 3:
                    print(f"  ⚠️  [DDJJ] Error consultando CUIT {cuit}: {e}")
                elif errores == 4:
                    print(f"  ⚠️  [DDJJ] Errores sucesivos, se omiten mensajes...")

        ok = (df.loc[mask, "verificada_dgr"] == "Sí").sum()
        no = (df.loc[mask, "verificada_dgr"] == "No").sum()
        err = df.loc[mask, "verificada_dgr"].isin(["Error", "Error login"]).sum()
        print(f"  ✅ Verificación DGR: {ok} confirmadas, {no} no encontradas, {err} errores")

    # ═════════════════════════════════════════════════════════════
    # PASE 2: Padrón Web ARCA/BC (pwContribBlockChain.do)
    # ═════════════════════════════════════════════════════════════
    if len(cuits_need_jur) > 0:
        print(f"  🔍 [Padrón] Consultando {len(cuits_need_jur)} CUITs en Padrón ARCA/BC...")
        dgr_init_padron_web(session)
        errores = 0

        for i, cuit in enumerate(cuits_need_jur, 1):
            if i % 10 == 0 or i == 1:
                print(f"  ⏳ [Padrón] Procesando {i}/{len(cuits_need_jur)}...")

            try:
                is_debug = debug_cuit and cuit == debug_cuit
                jur_val = dgr_jurisdicciones_cuit(session, cuit, debug=is_debug)
                time.sleep(delay)
                sel = mask_need_jur & (df["cuit"] == cuit)
                df.loc[sel, "jurisdicciones"] = jur_val
                # Sólo actualizar la fecha de check si la consulta fue exitosa
                if jur_val not in ("Error", "Error login"):
                    df.loc[sel, "jurisdicciones_check"] = today_str
            except Exception as e:
                errores += 1
                vacias = mask_need_jur & (df["cuit"] == cuit) & (df["jurisdicciones"].astype(str) == "")
                df.loc[vacias, "jurisdicciones"] = "Error"
                if errores <= 3:
                    print(f"  ⚠️  [Padrón] Error consultando CUIT {cuit}: {e}")
                elif errores == 4:
                    print(f"  ⚠️  [Padrón] Errores sucesivos, se omiten mensajes...")

        jur_series = df.loc[mask, "jurisdicciones"].astype(str)
        jur_ok = ((jur_series != "") & (~jur_series.isin(["Error", "Error login"]))).sum()
        jur_err = jur_series.isin(["Error", "Error login"]).sum()
        print(f"  ✅ Padrón ARCA: {jur_ok} CUITs consultados, {jur_err} errores")

    return df


# ═══════════════════════════════════════════════════════════════
# PASO 4: Generación de reporte HTML
# ═══════════════════════════════════════════════════════════════

def generate_report(df: pd.DataFrame, df_err: pd.DataFrame, start_date: str, end_date: str, con_dgr: bool, chart_data: dict | None = None) -> str:
    """Genera reporte HTML con tablas, KPIs y gráficos.

    df: DataFrame de presentaciones (con duplicados marcados, opcional verificación DGR)
    df_err: DataFrame de errores de validación (PS_error_validacion_dj)
    """

    # Timestamp en hora Argentina (America/Argentina/Buenos_Aires, UTC-3)
    # para que GitHub Actions (UTC) muestre la hora local correcta.
    generated = datetime.now(tz=ZoneInfo("America/Argentina/Buenos_Aires")).strftime("%d/%m/%Y %H:%M (ART)")

    # KPIs presentaciones
    total_registros = len(df)
    total_no_dup = (df["duplicado"] == "No").sum()
    total_dup = (df["duplicado"] == "Sí").sum()
    cuits_unicos = df["cuit"].nunique()

    # KPIs errores
    total_errores = len(df_err)
    cuits_errores = df_err["cuit"].nunique() if not df_err.empty else 0

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
            jur_cell = ""
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

                jv = r.get("jurisdicciones", "")
                if jv == "" or jv is None or (isinstance(jv, float) and pd.isna(jv)):
                    jur_cell = '<td>—</td>'
                elif jv in ("Error", "Error login"):
                    jur_cell = f'<td class="dgr-err">{jv}</td>'
                else:
                    jur_cell = f'<td>{jv}</td>'

            evento_label = EVENT_LABELS.get(r['nombre_evento'], r['nombre_evento'])
            estrellas = r.get('estrellas_valor', '') or ''
            feedback = r.get('texto_feedback', '') or ''
            texto_error = r.get('texto_del_error', '') or ''

            rows_html += f"""<tr{dup_class}>
                <td class="mono">{r['cuit']}</td>
                <td>{r['exact_timestamp']}</td>
                <td><code>{evento_label}</code></td>
                <td>{r['region']}</td>
                {jur_cell}
                <td>{r['total']}</td>
                <td>{estrellas}</td>
                <td>{feedback}</td>
                <td>{texto_error}</td>
                <td>{r['duplicado']}</td>
                {dgr_cell}
            </tr>"""
        return rows_html

    # Ordenar por timestamp descendente (más reciente primero)
    df_sorted = df.sort_values("exact_timestamp", ascending=False)
    table_all = build_table_rows(df_sorted)
    df_no_dup = df_sorted[df_sorted["duplicado"] == "No"].copy()
    table_no_dup = build_table_rows(df_no_dup, include_dgr=con_dgr)

    # ── Tabla errores de validación ──
    def build_err_rows(df_err_in: pd.DataFrame) -> str:
        if df_err_in.empty:
            return ""
        rows_html = ""
        for _, r in df_err_in.sort_values("exact_timestamp", ascending=False).iterrows():
            cuit = r.get('cuit', '') or ''
            ts = r.get('exact_timestamp', '') or ''
            region = r.get('region', '') or ''
            total = r.get('total', '') or ''
            texto_err = r.get('texto_del_error', '') or ''
            rows_html += f"""<tr>
                <td class="mono">{cuit}</td>
                <td>{ts}</td>
                <td>{region}</td>
                <td>{total}</td>
                <td>{texto_err}</td>
            </tr>"""
        return rows_html

    table_errores = build_err_rows(df_err)

    # ── Definición dinámica de columnas según con_dgr ──
    # Si con_dgr, "Jurisdicciones" se inserta entre Región y Total (pos 4),
    # corriendo los índices de Total..Duplicado en +1 y agregando DGR al final.
    if con_dgr:
        cols_no_dup = [
            "CUIT", "Timestamp", "Evento", "Región",
            "Jurisdicciones", "Total", "Estrellas", "Feedback",
            "Texto del Error", "Duplicado", "Verificada DGR",
        ]
    else:
        cols_no_dup = [
            "CUIT", "Timestamp", "Evento", "Región",
            "Total", "Estrellas", "Feedback", "Texto del Error", "Duplicado",
        ]
    cols_all = [
        "CUIT", "Timestamp", "Evento", "Región",
        "Total", "Estrellas", "Feedback", "Texto del Error", "Duplicado",
    ]
    cols_err = ["CUIT", "Timestamp", "Región", "Total", "Texto del Error"]

    def _build_th_rows(cols):
        """Genera las dos <tr>: la de headers con sort arrows y la de filtros."""
        hdrs, filts = [], []
        for i, name in enumerate(cols):
            if name == "Timestamp":
                cls = ' class="sort-active" data-dir="desc"'
                arrow = "&#x25BC;"
            else:
                cls = ""
                arrow = "&#x25B2;"
            hdrs.append(
                f'<th data-col="{i}"{cls}>{name} <span class="sort-arrow">{arrow}</span></th>'
            )
            filts.append(
                f'<th><input class="col-filter" data-col="{i}" placeholder="Filtrar..."></th>'
            )
        sep = "\n                        "
        return sep.join(hdrs), sep.join(filts)

    headers_no_dup_html, filters_no_dup_html = _build_th_rows(cols_no_dup)
    headers_all_html, filters_all_html = _build_th_rows(cols_all)
    headers_err_html, filters_err_html = _build_th_rows(cols_err)

    # Índices para el JS (recalculados al insertar Jurisdicciones)
    def _idx(cols, name, fallback=-1):
        return cols.index(name) if name in cols else fallback

    js_idx_estrellas_nodup = _idx(cols_no_dup, "Estrellas")
    js_idx_feedback_nodup = _idx(cols_no_dup, "Feedback")
    js_idx_dgr_nodup = _idx(cols_no_dup, "Verificada DGR")
    js_idx_duplicado_all = _idx(cols_all, "Duplicado")
    js_idx_cuit_all = _idx(cols_all, "CUIT")

    # ── KPI DGR extra ──
    dgr_kpi_html = ""
    if con_dgr:
        dgr_kpi_html = f"""
        <div class="kpi">
            <div class="label">No encontradas DGR</div>
            <div class="value v6" id="kpi-dgr-no">{dgr_no}</div>
        </div>"""

    # ── Datos para gráficos ──
    if chart_data:
        import json as _json
        chart_fechas_json = _json.dumps(chart_data["fechas"])
        chart_presentadas_no_dup_json = _json.dumps(chart_data.get("presentadas_no_dup", []))
        chart_enviadas_json = _json.dumps(chart_data["enc_enviadas"])
        chart_cerradas_json = _json.dumps(chart_data["enc_cerradas"])

        # Diferencia: presentadas_no_dup - (enc_enviadas + enc_cerradas)
        no_dup = chart_data.get("presentadas_no_dup", [])
        env = chart_data["enc_enviadas"]
        cer = chart_data["enc_cerradas"]
        diferencia = [
            no_dup[i] - (env[i] + cer[i]) if i < len(no_dup) else 0
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
        stopwords_json = _json.dumps(sorted(stopwords))
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
        stopwords_json = "[]"

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PS Verificación — COMARB</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
    /* Tema CLARO (default) */
    :root {{
        --bg: #f5f6fa; --surface: #ffffff; --surface2: #f0f2f7;
        --border: #dfe3ec; --text: #1a1d27; --text-dim: #6b7280;
        --accent: #4f6ef0; --green: #0d9f6e; --amber: #d97706;
        --red: #dc2e5c; --purple: #7c3aed; --cyan: #0891b2;
        --hover-tint: rgba(79,110,240,0.07);
        --row-border-soft: rgba(223,227,236,0.7);
        --chart-grid: rgba(100,116,139,0.18);
        --chart-text: #4b5563;
        --color-scheme: light;
        --radius: 12px;
    }}
    /* Tema OSCURO */
    [data-theme="dark"] {{
        --bg: #0f1117; --surface: #1a1d27; --surface2: #242836;
        --border: #2e3345; --text: #e4e6f0; --text-dim: #8b90a5;
        --accent: #6c8aff; --green: #45d9a8; --amber: #f59e42;
        --red: #ef5678; --purple: #a78bfa; --cyan: #38bdf8;
        --hover-tint: rgba(108,138,255,0.04);
        --row-border-soft: rgba(46,51,69,0.5);
        --chart-grid: #2e3345;
        --chart-text: #8b90a5;
        --color-scheme: dark;
    }}
    html {{ color-scheme: var(--color-scheme); }}
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
    .header-row {{
        display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem;
    }}
    .theme-toggle {{
        background: var(--surface); color: var(--text);
        border: 1px solid var(--border); border-radius: 10px;
        padding: .5rem .9rem; cursor: pointer;
        font-family: inherit; font-size: .85rem; font-weight: 500;
        display: inline-flex; align-items: center; gap: .4rem;
        transition: background .2s, color .2s, border-color .2s;
        white-space: nowrap;
    }}
    .theme-toggle:hover {{ color: var(--accent); border-color: var(--accent); }}
    .theme-toggle .theme-icon {{ font-size: 1rem; }}
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
    tr:hover td {{ background: var(--hover-tint); }}
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

    /* Tabs principales (errores / presentaciones) — estilo underline para
       distinguir visualmente del nivel anidado de pestañas dentro de Presentaciones */
    .main-tabs {{
        display: flex; gap: 1.5rem; margin: 1.5rem 0 2rem;
        border-bottom: 2px solid var(--border);
    }}
    .main-tab {{
        padding: .8rem 0; cursor: pointer;
        font-size: 1rem; font-weight: 600; color: var(--text-dim);
        border-bottom: 3px solid transparent;
        margin-bottom: -2px; transition: color .2s, border-color .2s;
    }}
    .main-tab:hover {{ color: var(--text); }}
    .main-tab.active {{
        color: var(--accent); border-bottom-color: var(--accent);
    }}
    .main-tab-content {{ display: none; }}
    .main-tab-content.active {{ display: block; }}

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
        padding: .25rem .6rem; border-bottom: 1px solid var(--row-border-soft);
        vertical-align: middle; white-space: nowrap;
    }}
    .bar-table tbody tr:hover td {{ background: var(--hover-tint); }}
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
    .bar-table .dif-pos {{ color: var(--text); font-weight: 600; }}
    .bar-table .dif-neg {{ color: var(--red); font-weight: 600; }}

    /* Anchos específicos para la tabla de errores por día:
       día fijo y estrecho (fecha siempre tiene 10 chars),
       cantidad ocupa la mayoría del espacio (barra), CUIT a la derecha */
    #errBarTable {{ table-layout: fixed; }}
    #errBarTable th:nth-child(1), #errBarTable td:nth-child(1) {{ width: 110px; }}
    #errBarTable th:nth-child(2), #errBarTable td:nth-child(2) {{ width: auto; padding-right: 2.5rem; }}
    #errBarTable th:nth-child(3), #errBarTable td:nth-child(3) {{ width: 220px; padding-left: 2rem; }}
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
        min-width: 0; max-width: 100%;
        overflow: hidden;
        box-sizing: border-box;
    }}
    .word-cloud span {{
        cursor: default; transition: opacity .2s;
        font-weight: 500;
    }}
    .word-cloud span:hover {{ opacity: .7; }}
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/html2pdf.js@0.10.2/dist/html2pdf.bundle.min.js"></script>
<!-- html2pdf bundles html2canvas+jsPDF internally pero no los re-exporta; los cargamos
     standalone para usarlos directo desde generarPDF() (captura por page-group). -->
<script src="https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/jspdf@2.5.1/dist/jspdf.umd.min.js"></script>
</head>
<body>
<div class="container">

<header>
    <div class="header-row">
        <div>
            <h1>📋 Presentación Simplificada</h1>
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
        </div>
        <div style="display:flex;flex-direction:column;gap:.5rem;align-items:flex-end">
            <button id="pdf-download" class="theme-toggle" aria-label="Descargar PDF" title="Descargar PDF (KPIs y gráficos)">
                <span class="theme-icon">📄</span> Descargar PDF
            </button>
            <button id="theme-toggle" class="theme-toggle" aria-label="Cambiar tema" title="Cambiar tema"></button>
        </div>
    </div>
</header>

<div class="main-tabs">
    <div class="main-tab active" onclick="switchMainTab('presentaciones')">Presentaciones (<span id="main-tab-count-presentaciones">{total_registros}</span>)</div>
    <div class="main-tab" onclick="switchMainTab('errores')">Errores de validación (<span id="main-tab-count-errores">{total_errores}</span>)</div>
</div>

<div id="main-tab-errores" class="main-tab-content">
    <div class="kpis">
        <div class="kpi">
            <div class="label">Errores totales</div>
            <div class="value v6" id="kpi-err-total">{total_errores}</div>
        </div>
        <div class="kpi">
            <div class="label">CUITs con errores</div>
            <div class="value v2" id="kpi-err-cuits">{cuits_errores}</div>
        </div>
    </div>

    <div class="charts-grid">
        <div class="chart-card full">
            <h3>Errores de validación por día</h3>
            <div class="bar-table-wrap" style="max-height:420px">
                <table class="bar-table" id="errBarTable">
                    <thead>
                        <tr>
                            <th>Día</th>
                            <th>Cantidad</th>
                            <th>CUIT con más errores</th>
                        </tr>
                    </thead>
                    <tbody id="errBarTableBody"></tbody>
                </table>
            </div>
        </div>
        <div class="chart-card full">
            <h3>Top textos de error</h3>
            <div style="position:relative;height:480px;">
                <canvas id="chartErrTop"></canvas>
            </div>
        </div>
    </div>

    <div class="card">
        <table id="tbl-errores">
            <thead>
                <tr>
                    {headers_err_html}
                </tr>
                <tr class="filter-row">
                    {filters_err_html}
                </tr>
            </thead>
            <tbody>{table_errores}</tbody>
        </table>
    </div>
</div>

<div id="main-tab-presentaciones" class="main-tab-content active">
<div class="kpis">
    <div class="kpi">
        <div class="label">Total registros</div>
        <div class="value v1" id="kpi-total">{total_registros}</div>
    </div>
    <div class="kpi">
        <div class="label">CUITs únicos</div>
        <div class="value v2" id="kpi-cuits">{cuits_unicos}</div>
    </div>
    <div class="kpi">
        <div class="label">No duplicados</div>
        <div class="value v3" id="kpi-no-dup">{total_no_dup}</div>
    </div>
    <div class="kpi">
        <div class="label">Duplicados</div>
        <div class="value v4" id="kpi-dup">{total_dup}</div>
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
        <div class="tab active" onclick="switchTab('no-dup')">Sin duplicados (<span id="tab-count-no-dup">{total_no_dup}</span>)</div>
        <div class="tab" onclick="switchTab('all')">Con duplicados (<span id="tab-count-all">{total_registros}</span>)</div>
    </div>

    <div id="tab-no-dup" class="tab-content active">
        <div class="card">
            <table id="tbl-no-dup">
                <thead>
                    <tr>
                        {headers_no_dup_html}
                    </tr>
                    <tr class="filter-row">
                        {filters_no_dup_html}
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
                        {headers_all_html}
                    </tr>
                    <tr class="filter-row">
                        {filters_all_html}
                    </tr>
                </thead>
                <tbody>{table_all}</tbody>
            </table>
        </div>
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

function switchMainTab(id) {{
    document.querySelectorAll('.main-tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.main-tab').forEach(el => el.classList.remove('active'));
    document.getElementById('main-tab-' + id).classList.add('active');
    event.target.classList.add('active');
    /* Re-render word cloud cuando la pestaña "Presentaciones" se vuelve visible
       (al cargar la página está oculta y clientWidth=0 hace que el layout falle) */
    if (id === 'presentaciones' && cloudEl && cloudEl.children.length === 0) {{
        renderWordCloud(_lastFeedbackTexts);
    }}
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

function onFiltersChanged() {{
    applyFiltersAll();
    recomputeKPIsAndCharts();
}}

document.querySelectorAll('.col-filter').forEach(input => {{
    input.addEventListener('input', onFiltersChanged);
}});

/* Inputs del rango de fechas */
const fechaDesdeEl = document.getElementById('fecha-desde');
const fechaHastaEl = document.getElementById('fecha-hasta');
const periodResetEl = document.getElementById('period-reset');
const defaultDesde = fechaDesdeEl.value;
const defaultHasta = fechaHastaEl.value;

fechaDesdeEl.addEventListener('change', onFiltersChanged);
fechaHastaEl.addEventListener('change', onFiltersChanged);
periodResetEl.addEventListener('click', () => {{
    fechaDesdeEl.value = defaultDesde;
    fechaHastaEl.value = defaultHasta;
    onFiltersChanged();
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
            const aNum = Number(aText);
            const bNum = Number(bText);
            const aIsNum = aText !== '' && !isNaN(aNum);
            const bIsNum = bText !== '' && !isNaN(bNum);
            let cmp;
            if (aIsNum && bIsNum) {{
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
Chart.defaults.font.family = "'DM Sans', sans-serif";

/* Helper: leer colores del tema desde CSS vars (re-evalúa al cambiar tema) */
function getThemeColors() {{
    const style = getComputedStyle(document.documentElement);
    return {{
        gridColor: (style.getPropertyValue('--chart-grid') || '').trim() || '#2e3345',
        textColor: (style.getPropertyValue('--chart-text') || '').trim() || '#8b90a5',
        textDim:   (style.getPropertyValue('--text-dim') || '').trim() || '#8b90a5',
    }};
}}
Chart.defaults.color = getThemeColors().textColor;

/* Datos base (período completo) */
const RAW_FECHAS = {chart_fechas_json};
const RAW_PRES = {chart_presentadas_no_dup_json};
const RAW_ENV = {chart_enviadas_json};
const RAW_CERR = {chart_cerradas_json};
const STOPWORDS = new Set({stopwords_json});

/* Instancias de Chart.js */
let _chartEstrellas = null;
let _chartProporcion = null;
let _chartErrTop = null;

const cloudEl = document.getElementById('wordCloud');

function renderBarTable(fechas, pres, env, cerr) {{
    const tbody = document.getElementById('barTableBody');
    tbody.innerHTML = '';
    if (fechas.length === 0) return;
    const maxVal = Math.max(...pres, ...env, ...cerr, 1);
    const barPct = v => Math.max(0, (v / maxVal) * 100);
    const barHtml = (val, color) =>
        '<div class="bar-cell"><span class="bar-val">' + val +
        '</span><span class="bar" style="width:' + barPct(val) +
        '%;background:' + color + '"></span></div>';
    for (let i = fechas.length - 1; i >= 0; i--) {{
        const tr = document.createElement('tr');
        const difVal = pres[i] - (env[i] + cerr[i]);
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

function renderErrBarTable(fechas, counts, cuitRankingByDate) {{
    /* fechas: array de strings YYYY-MM-DD
       counts: array alineado con fechas, conteo total de errores por día
       cuitRankingByDate: Map<fecha, Array<[cuit, count]>> ordenada por count desc */
    const tbody = document.getElementById('errBarTableBody');
    tbody.innerHTML = '';
    if (fechas.length === 0) return;
    const maxVal = Math.max(...counts, 1);
    const barPct = v => Math.max(0, (v / maxVal) * 100);
    const barHtml = (val, color) =>
        '<div class="bar-cell"><span class="bar-val">' + val +
        '</span><span class="bar" style="width:' + barPct(val) +
        '%;background:' + color + '"></span></div>';
    for (let i = fechas.length - 1; i >= 0; i--) {{
        const tr = document.createElement('tr');
        const ranking = cuitRankingByDate && cuitRankingByDate.get(fechas[i]);
        let topCuitStr = '—';
        if (ranking && ranking.length > 0) {{
            const [topCuit, topCount] = ranking[0];
            topCuitStr = '<span class="mono" style="font-size:.78rem">' + topCuit +
                '</span> <span style="color:var(--text-dim);font-size:.78rem">(' + topCount + ')</span>';
        }}
        tr.innerHTML =
            '<td style="font-family:JetBrains Mono,monospace;font-size:.75rem">' + fechas[i] + '</td>' +
            '<td>' + barHtml(counts[i], '#ef5678') + '</td>' +
            '<td>' + topCuitStr + '</td>';
        tbody.appendChild(tr);
    }}
}}

function renderErrTopChart(topPairs) {{
    /* topPairs: array de [textoError, count] ordenado desc, máx 10.
       Las etiquetas del eje Y se envuelven en hasta 3 líneas para que el
       texto completo sea visible sin truncar. */
    if (_chartErrTop) {{ _chartErrTop.destroy(); _chartErrTop = null; }}
    const __t = getThemeColors();
    Chart.defaults.color = __t.textColor;
    const canvas = document.getElementById('chartErrTop');
    if (!canvas) return;
    if (!topPairs || topPairs.length === 0) {{
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        return;
    }}
    /* Wrap a palabra en N líneas de ~maxChars cada una */
    const wrapLabel = (s, maxChars, maxLines) => {{
        const words = (s || '').split(/\s+/);
        const lines = [];
        let cur = '';
        for (const w of words) {{
            if ((cur + ' ' + w).trim().length <= maxChars) {{
                cur = (cur + ' ' + w).trim();
            }} else {{
                if (cur) lines.push(cur);
                cur = w;
                if (lines.length >= maxLines - 1) {{
                    /* Meto el resto en la última línea, truncando con … si hace falta */
                    const rest = words.slice(words.indexOf(w)).join(' ');
                    lines.push(rest.length > maxChars ? rest.slice(0, maxChars - 1) + '…' : rest);
                    return lines;
                }}
            }}
        }}
        if (cur) lines.push(cur);
        return lines;
    }};
    const labels = topPairs.map(p => wrapLabel(p[0], 40, 3));
    const fullLabels = topPairs.map(p => p[0]);
    const values = topPairs.map(p => p[1]);
    _chartErrTop = new Chart(canvas, {{
        type: 'bar',
        data: {{
            labels: labels,
            datasets: [{{
                data: values,
                backgroundColor: '#ef5678cc',
                borderColor: '#ef5678',
                borderWidth: 1,
                borderRadius: 4,
            }}],
        }},
        options: {{
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            plugins: {{
                legend: {{ display: false }},
                tooltip: {{
                    callbacks: {{
                        title: items => fullLabels[items[0].dataIndex],
                    }},
                }},
            }},
            scales: {{
                x: {{ grid: {{ color: __t.gridColor }}, beginAtZero: true, ticks: {{ precision: 0 }} }},
                y: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 11 }}, autoSkip: false }} }},
            }},
        }},
    }});
}}

function renderEstrellasChart(countsByLabel) {{
    /* countsByLabel: {{'5': n, '4': n, '3': n, '2': n, '1': n}} */
    if (_chartEstrellas) {{ _chartEstrellas.destroy(); _chartEstrellas = null; }}
    const __t = getThemeColors();
    Chart.defaults.color = __t.textColor;
    const allLabels = ['5', '4', '3', '2', '1'];
    const labels = [];
    const values = [];
    allLabels.forEach(l => {{
        if ((countsByLabel[l] || 0) > 0) {{ labels.push(l); values.push(countsByLabel[l]); }}
    }});
    if (labels.length === 0) return;
    const colorMap = {{'1': '#ef5678', '2': '#f59e42', '3': '#f59e42', '4': '#45d9a8', '5': '#6c8aff'}};
    const colors = labels.map(l => colorMap[l] || '#8b90a5');
    _chartEstrellas = new Chart(document.getElementById('chartEstrellas'), {{
        type: 'bar',
        data: {{
            labels: labels.map(l => l + ' ★'),
            datasets: [{{
                data: values,
                backgroundColor: colors.map(c => c + 'cc'),
                borderColor: colors,
                borderWidth: 1,
                borderRadius: 6,
            }}],
        }},
        options: {{
            responsive: true,
            plugins: {{ legend: {{ display: false }} }},
            scales: {{
                x: {{ grid: {{ display: false }} }},
                y: {{ grid: {{ color: __t.gridColor }}, beginAtZero: true }},
            }},
        }},
    }});
}}

function renderProporcionChart(cinco, resto) {{
    if (_chartProporcion) {{ _chartProporcion.destroy(); _chartProporcion = null; }}
    if (cinco + resto === 0) return;
    Chart.defaults.color = getThemeColors().textColor;
    _chartProporcion = new Chart(document.getElementById('chartProporcion'), {{
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

let _lastFeedbackTexts = [];
function renderWordCloud(feedbackTexts) {{
    _lastFeedbackTexts = feedbackTexts;
    cloudEl.innerHTML = '';
    const wordCount = {{}};
    const re = /\\b[a-záéíóúñü]{{3,}}\\b/g;
    feedbackTexts.forEach(t => {{
        const words = (t || '').toLowerCase().match(re) || [];
        words.forEach(w => {{
            if (!STOPWORDS.has(w)) wordCount[w] = (wordCount[w] || 0) + 1;
        }});
    }});
    const entries = Object.entries(wordCount).sort((a, b) => b[1] - a[1]).slice(0, 45);
    if (entries.length === 0) {{
        cloudEl.innerHTML = '<span style="color:var(--text-dim);font-size:.85rem;position:relative">Sin datos de feedback</span>';
        return;
    }}
    /* Si el contenedor aún no tiene ancho real (tab oculta), diferir el render.
       Al cambiar a la pestaña "Presentaciones" se vuelve a llamar con el ancho correcto. */
    const Wreal = cloudEl.clientWidth;
    if (!Wreal || Wreal < 50) {{
        return;
    }}
    const maxCount = entries[0][1];
    const wordData = entries.map(([w, c]) => ({{
        text: w,
        size: Math.max(14, Math.floor(55 * c / maxCount)),
        count: c,
    }}));

    const colors = ['#6c8aff','#45d9a8','#a78bfa','#38bdf8','#f59e42','#ef5678','#e4e6f0'];
    const W = Wreal;
    const H = cloudEl.clientHeight || 380;
    const placed = [];

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

    const shuffled = [...wordData].sort(() => Math.random() - 0.5);
    const cx = W / 2, cy = H / 2;

    shuffled.forEach((w, i) => {{
        const dim = measure(w.text, w.size);
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
}}

function getVisibleRows(tableId) {{
    return Array.from(document.querySelectorAll('#' + tableId + ' tbody tr')).filter(r => r.style.display !== 'none');
}}

function recomputeKPIsAndCharts() {{
    /* KPIs desde tbl-all visible */
    const allRows = getVisibleRows('tbl-all');
    const total = allRows.length;
    let noDupCount = 0, dupCount = 0;
    const cuitsSet = new Set();
    allRows.forEach(r => {{
        const cells = r.querySelectorAll('td');
        cuitsSet.add((cells[{js_idx_cuit_all}]?.textContent || '').trim());
        const d = (cells[{js_idx_duplicado_all}]?.textContent || '').trim();
        if (d === 'No') noDupCount++; else if (d === 'Sí') dupCount++;
    }});
    document.getElementById('kpi-total').textContent = total;
    document.getElementById('kpi-cuits').textContent = cuitsSet.size;
    document.getElementById('kpi-no-dup').textContent = noDupCount;
    document.getElementById('kpi-dup').textContent = dupCount;
    document.getElementById('tab-count-no-dup').textContent = noDupCount;
    document.getElementById('tab-count-all').textContent = total;

    /* KPI DGR no (desde tbl-no-dup visible) */
    const noDupRows = getVisibleRows('tbl-no-dup');
    const dgrKpi = document.getElementById('kpi-dgr-no');
    if (dgrKpi) {{
        let dgrNo = 0;
        noDupRows.forEach(r => {{
            const cells = r.querySelectorAll('td');
            if ((cells[{js_idx_dgr_nodup}]?.textContent || '').trim() === 'No') dgrNo++;
        }});
        dgrKpi.textContent = dgrNo;
    }}

    /* Bar table: filtrar RAW_* por rango de fecha actual */
    const desde = document.getElementById('fecha-desde').value;
    const hasta = document.getElementById('fecha-hasta').value;
    const fechas = [];
    const pres = [];
    const env = [];
    const cerr = [];
    RAW_FECHAS.forEach((f, i) => {{
        if ((!desde || f >= desde) && (!hasta || f <= hasta)) {{
            fechas.push(f);
            pres.push(RAW_PRES[i] || 0);
            env.push(RAW_ENV[i] || 0);
            cerr.push(RAW_CERR[i] || 0);
        }}
    }});
    renderBarTable(fechas, pres, env, cerr);

    /* Estrellas y feedback: derivar de filas visibles de tbl-no-dup */
    const estCounts = {{'1': 0, '2': 0, '3': 0, '4': 0, '5': 0}};
    const feedbackTexts = [];
    noDupRows.forEach(r => {{
        const cells = r.querySelectorAll('td');
        const est = (cells[{js_idx_estrellas_nodup}]?.textContent || '').trim();
        if (estCounts.hasOwnProperty(est)) estCounts[est]++;
        const fb = (cells[{js_idx_feedback_nodup}]?.textContent || '').trim();
        if (fb) feedbackTexts.push(fb);
    }});
    renderEstrellasChart(estCounts);
    renderProporcionChart(estCounts['5'], estCounts['1'] + estCounts['2'] + estCounts['3'] + estCounts['4']);
    renderWordCloud(feedbackTexts);

    /* ── Errores de validación (tab principal "Errores") ── */
    const errRows = getVisibleRows('tbl-errores');
    const errCuitsSet = new Set();
    const errPorDia = {{}};
    const errTextCounts = {{}};
    /* cuitsPorDia[fecha][cuit] = count, para ranking de CUIT con más errores por día */
    const cuitsPorDia = {{}};
    errRows.forEach(r => {{
        const cells = r.querySelectorAll('td');
        const cuit = (cells[0]?.textContent || '').trim();
        if (cuit) errCuitsSet.add(cuit);
        const ts = (cells[1]?.textContent || '').trim().slice(0, 10);
        if (ts) errPorDia[ts] = (errPorDia[ts] || 0) + 1;
        if (ts && cuit) {{
            if (!cuitsPorDia[ts]) cuitsPorDia[ts] = {{}};
            cuitsPorDia[ts][cuit] = (cuitsPorDia[ts][cuit] || 0) + 1;
        }}
        const txt = (cells[4]?.textContent || '').trim();
        if (txt) errTextCounts[txt] = (errTextCounts[txt] || 0) + 1;
    }});
    document.getElementById('kpi-err-total').textContent = errRows.length;
    document.getElementById('kpi-err-cuits').textContent = errCuitsSet.size;
    document.getElementById('main-tab-count-errores').textContent = errRows.length;
    document.getElementById('main-tab-count-presentaciones').textContent = total;

    /* Ranking: CUIT con más errores por día (uno por fecha) para la 4ta columna */
    const rankingByDate = new Map();
    for (const [fecha, cuitsMap] of Object.entries(cuitsPorDia)) {{
        const sorted = Object.entries(cuitsMap).sort((a, b) => b[1] - a[1]);
        rankingByDate.set(fecha, sorted);
    }}

    const errFechas = Object.keys(errPorDia).sort();
    const errCounts = errFechas.map(f => errPorDia[f]);
    renderErrBarTable(errFechas, errCounts, rankingByDate);

    const topErr = Object.entries(errTextCounts).sort((a, b) => b[1] - a[1]).slice(0, 10);
    renderErrTopChart(topErr);
}}

/* Render inicial (usa el período completo por defecto) */
recomputeKPIsAndCharts();

/* ── Toggle de tema (claro / oscuro) ── */
const THEME_KEY = 'ps_verificacion_theme';
const themeBtn = document.getElementById('theme-toggle');

function applyTheme(theme) {{
    const t = (theme === 'dark') ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', t);
    if (themeBtn) {{
        // En tema claro mostramos 🌙 (invita a pasar a oscuro), y viceversa
        themeBtn.innerHTML = (t === 'dark')
            ? '<span class="theme-icon">☀️</span> Modo claro'
            : '<span class="theme-icon">🌙</span> Modo oscuro';
        themeBtn.title = (t === 'dark') ? 'Cambiar a tema claro' : 'Cambiar a tema oscuro';
    }}
    try {{ localStorage.setItem(THEME_KEY, t); }} catch (_) {{ }}
    // Re-render charts y KPIs con los nuevos colores de grid/texto
    Chart.defaults.color = getThemeColors().textColor;
    if (typeof recomputeKPIsAndCharts === 'function') recomputeKPIsAndCharts();
}}

let savedTheme = 'light';
try {{ savedTheme = localStorage.getItem(THEME_KEY) || 'light'; }} catch (_) {{ }}
applyTheme(savedTheme);

if (themeBtn) {{
    themeBtn.addEventListener('click', () => {{
        const current = document.documentElement.getAttribute('data-theme') || 'light';
        applyTheme(current === 'dark' ? 'light' : 'dark');
    }});
}}

/* ── Descarga de PDF (html2canvas + jsPDF, captura per-page-group) ──
   Approach: hacer visibles todas las pestañas (Chart.js renderiza canvases),
   ocultar UI no relevante, ejecutar la lógica fit-to-page que decide qué cards
   van juntas, y luego capturar cada page-group con html2canvas + armar el PDF
   manualmente con jsPDF. La card .pdf-allow-split (Presentadas) se slicéa en
   N páginas según altura. Evita el slicing de canvas de html2pdf que dejaba
   contenido pegado al borde inferior con whitespace al top. */
const pdfBtn = document.getElementById('pdf-download');
if (pdfBtn) {{
    pdfBtn.addEventListener('click', () => {{
        if (typeof html2canvas === 'undefined' || (typeof window.jspdf === 'undefined' && typeof window.jsPDF === 'undefined')) {{
            alert('html2canvas / jsPDF no cargaron. Revisá tu conexión a internet.');
            return;
        }}
        generarPDF();
    }});
}}

async function generarPDF() {{
    const labelOriginal = pdfBtn.innerHTML;
    pdfBtn.disabled = true;
    pdfBtn.innerHTML = '<span class="theme-icon">⏳</span> Generando…';

    // Overlay que tapa el layout shift mientras se genera
    const overlay = document.createElement('div');
    overlay.style.cssText = (
        'position:fixed; inset:0; background:rgba(15,17,23,0.85); ' +
        'z-index:99999; display:flex; align-items:center; justify-content:center; ' +
        'color:white; font-family:\\'DM Sans\\', sans-serif; font-size:1.1rem;'
    );
    overlay.innerHTML = '<div style="text-align:center"><div style="font-size:2rem;margin-bottom:.5rem">📄</div>Generando PDF…</div>';
    document.body.appendChild(overlay);

    // Guardar tema y forzar claro
    const prevTheme = document.documentElement.getAttribute('data-theme') || 'light';
    if (prevTheme !== 'light') {{
        document.documentElement.setAttribute('data-theme', 'light');
    }}

    // Acciones de restore
    const restoreActions = [];

    // 1. Ajustar ancho del container para A4 LANDSCAPE (~277mm útiles ≈ 1047px @ 96dpi).
    //    Usamos 1000px para tener aire y headers anchos de tabla entren cómodos.
    const container = document.querySelector('.container');
    if (container) {{
        const origCont = {{
            maxWidth: container.style.maxWidth,
            width: container.style.width,
            padding: container.style.padding,
            margin: container.style.margin,
        }};
        restoreActions.push(() => {{
            container.style.maxWidth = origCont.maxWidth;
            container.style.width = origCont.width;
            container.style.padding = origCont.padding;
            container.style.margin = origCont.margin;
        }});
        container.style.maxWidth = '1000px';
        container.style.width = '1000px';
        container.style.padding = '0';
        container.style.margin = '0';
    }}
    const origBodyPadding = document.body.style.padding;
    restoreActions.push(() => {{ document.body.style.padding = origBodyPadding; }});
    document.body.style.padding = '0';

    // 2. Inyectar CSS para que cards, kpi-grids y filas de tabla NO se corten
    //    entre páginas. Esto fuerza a html2pdf a empujar el elemento entero
    //    a la página siguiente si no entra al final de la actual.
    const pdfStyleEl = document.createElement('style');
    pdfStyleEl.id = 'pdf-page-break-rules';
    pdfStyleEl.textContent = (
        // Cards y kpis no se parten entre páginas, EXCEPTO los marcados
        // con .pdf-allow-split (ej. la tabla larga de Presentadas).
        '.card:not(.pdf-allow-split), .chart-card:not(.pdf-allow-split), .kpis {{ page-break-inside: avoid !important; break-inside: avoid !important; }}' +
        'tr, thead {{ page-break-inside: avoid !important; break-inside: avoid !important; }}' +
        // Tablas regulares (no bar-table): permitir wrap natural en palabras enteras
        'table:not(.bar-table) th, table:not(.bar-table) td {{ white-space: normal !important; }}' +
        // Compactar tablas regulares para que entren más filas por página
        'table:not(.bar-table) {{ font-size: 0.78rem !important; }}' +
        'table:not(.bar-table) td, table:not(.bar-table) th {{ padding: 0.3rem 0.6rem !important; line-height: 1.35 !important; }}' +
        // Padding de cards: las normales 1rem, las que permiten split (Presentadas)
        // padding mínimo para que la tabla suba al top de la página.
        '.card:not(.pdf-allow-split), .chart-card:not(.pdf-allow-split) {{ padding: 1rem !important; }}' +
        '.card.pdf-allow-split, .chart-card.pdf-allow-split {{ padding: 0 !important; margin: 0 !important; }}' +
        // Title de la card de Presentadas: compacto pero visible
        '.chart-card.pdf-allow-split h2, .chart-card.pdf-allow-split h3, .chart-card.pdf-allow-split .section-title, .chart-card.pdf-allow-split .card-title {{ margin: 0 !important; padding: 0.2rem 0.4rem !important; font-size: 0.72rem !important; line-height: 1.1 !important; }}' +
        '.chart-card.pdf-allow-split .bar-table-wrap {{ margin: 0 !important; padding: 0 !important; }}'
    );
    document.head.appendChild(pdfStyleEl);
    restoreActions.push(() => {{ pdfStyleEl.remove(); }});

    // 3. Cap el alto de los wrappers de bar-table a ~una página de A4 landscape
    //    (188mm útiles ≈ 711px @ 96dpi, descontando KPIs + título + paddings ≈ 410px).
    //    EXCEPCIÓN: la card "Presentadas..." muestra todos los días (puede
    //    ocupar varias páginas). Se identifica por estar dentro de un .chart-card
    //    cuyo título empieza con "presentadas".
    document.querySelectorAll('.bar-table-wrap').forEach(el => {{
        const origMH = el.style.maxHeight;
        const origO = el.style.overflow;
        restoreActions.push(() => {{
            el.style.maxHeight = origMH;
            el.style.overflow = origO;
        }});
        // ¿Está dentro de la card Presentadas?
        const parentCard = el.closest('.chart-card, .card');
        const cardTitle = (parentCard?.querySelector('.section-title, h2, h3')?.textContent || '').trim().toLowerCase();
        if (cardTitle.startsWith('presentadas')) {{
            // Sin cap: con la captura per-page-group, la tabla se slicéa manualmente
            // a páginas A4-landscape después de capturar. Todos los días entran.
            el.style.maxHeight = 'none';
            el.style.overflow = 'visible';
            // Marcar la card para que el slicer la maneje aparte (no entra al stack).
            if (parentCard) {{
                parentCard.classList.add('pdf-allow-split');
                restoreActions.push(() => parentCard.classList.remove('pdf-allow-split'));
            }}
        }} else {{
            el.style.maxHeight = '410px';
            el.style.overflow = 'hidden';
        }}
    }});

    // 3b. (Detección de unidades atómicas se mueve DESPUÉS del show-tabs + charts)

    // 3. Mostrar TODAS las main-tab-content y tab-content (para que Chart.js renderice)
    document.querySelectorAll('.main-tab-content, .tab-content').forEach(el => {{
        const orig = el.style.display;
        restoreActions.push(() => {{ el.style.display = orig; }});
        el.style.display = 'block';
    }});

    // 4. Ocultar UI no relevante para PDF
    const hideSelectors = [
        '#theme-toggle', '#pdf-download',
        '.tabs', '.main-tabs',
        '.period-filter button',
        '.col-filter', '.filter-row',
        '#tbl-no-dup', '#tbl-all', '#tbl-errores',
        // Pie de informe
        '.container > footer', 'body > footer',
    ];
    hideSelectors.forEach(sel => {{
        document.querySelectorAll(sel).forEach(el => {{
            const orig = el.style.display;
            restoreActions.push(() => {{ el.style.display = orig; }});
            el.style.display = 'none';
        }});
    }});

    // 5. Re-render charts (ahora que sus parents son visibles y con nuevo ancho)
    if (typeof recomputeKPIsAndCharts === 'function') recomputeKPIsAndCharts();

    // 6. Esperar a que Chart.js termine de renderizar
    await new Promise(r => setTimeout(r, 700));

    // 6.0. Ocultar cards "fantasma" SOLO si están al FINAL del documento
    //      (después del último elemento real). Los cards vacíos del medio
    //      (separadores entre tabs, etc.) se mantienen para no romper layout.
    {{
        const realEls = [...document.querySelectorAll('.chart-card, .kpis')]
            .filter(el => el.getBoundingClientRect().height > 50);
        const maxBottom = realEls.length > 0
            ? Math.max(...realEls.map(el => el.getBoundingClientRect().bottom))
            : 0;
        document.querySelectorAll('.container .card').forEach(el => {{
            const r = el.getBoundingClientRect();
            const txt = el.textContent.replace(/\\s+/g, '').trim();
            // Sólo ocultar si está al final (su top >= maxBottom) Y es vacío/chico
            if (r.top >= maxBottom - 10 && (r.height < 80 || txt.length < 5)) {{
                const orig = el.style.display;
                restoreActions.push(() => {{ el.style.display = orig; }});
                el.style.display = 'none';
            }}
        }});
    }}

    // 6a-pre. Reordenar tabs principales: mover #main-tab-presentaciones ANTES
    //         de #main-tab-errores para que en el PDF aparezca primero todo lo
    //         de presentaciones (KPIs2 + encuesta + presentadas) y después lo
    //         de errores (KPIs1 + errores por día + top textos).
    const presTab = document.getElementById('main-tab-presentaciones');
    const errTab = document.getElementById('main-tab-errores');
    if (presTab && errTab && presTab.parentElement === errTab.parentElement) {{
        const tabsParent = presTab.parentElement;
        const presNextSibling = presTab.nextSibling;
        if (presTab !== errTab && errTab.compareDocumentPosition(presTab) & Node.DOCUMENT_POSITION_FOLLOWING) {{
            // presTab está DESPUÉS de errTab → moverlo antes
            tabsParent.insertBefore(presTab, errTab);
            restoreActions.push(() => {{
                if (presNextSibling) tabsParent.insertBefore(presTab, presNextSibling);
                else tabsParent.appendChild(presTab);
            }});
            await new Promise(r => setTimeout(r, 50));
        }}
    }}

    // 6a. Reordenar: mover la card "Encuesta valoraciones" ANTES de la card
    //     "Presentadas..." para que quede en la página de KPIs2.
    //     OJO: la card "Presentadas" también contiene la palabra "Encuestas"
    //     en su título (ENCUESTAS ENVIADAS), así que usamos match más estricto.
    const allChartCards = [...document.querySelectorAll('.chart-card')];
    const encuestaCard = allChartCards.find(c => {{
        const t = (c.querySelector('.section-title, h2, h3')?.textContent || '').trim().toLowerCase();
        return t.startsWith('encuesta:') || t.includes('valoraciones');
    }});
    const presentadasCard = allChartCards.find(c => {{
        const t = (c.querySelector('.section-title, h2, h3')?.textContent || '').trim().toLowerCase();
        return t.startsWith('presentadas');
    }});
    if (encuestaCard && presentadasCard && encuestaCard.parentElement === presentadasCard.parentElement
        && encuestaCard !== presentadasCard) {{
        const origParent = encuestaCard.parentElement;
        const origNextSibling = encuestaCard.nextSibling;
        origParent.insertBefore(encuestaCard, presentadasCard);
        restoreActions.push(() => {{
            if (origNextSibling) origParent.insertBefore(encuestaCard, origNextSibling);
            else origParent.appendChild(encuestaCard);
        }});
        await new Promise(r => setTimeout(r, 50));
    }}

    // 6b. Lógica fit-to-page: cards consecutivas que sumadas entran en una
    //     página A4 landscape quedan juntas; sino se rompe página.
    //     Incluye .kpis para que los bloques de KPIs cuenten en el cálculo.
    const PAGE_USABLE_PX = 620;
    const CARD_GAP_PX = 24;
    const atomicCards = [...document.querySelectorAll('.chart-card, .container .card, .kpis')]
        .filter(c => c.getBoundingClientRect().height > 50)
        .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
    let pageAccumH = 0;
    atomicCards.forEach((c, i) => {{
        const h = c.getBoundingClientRect().height;
        const allowSplit = c.classList.contains('pdf-allow-split');
        const origPB = c.style.pageBreakBefore;
        const origBB = c.style.breakBefore;
        const origPI = c.style.pageBreakInside;
        const origBI = c.style.breakInside;
        restoreActions.push(() => {{
            c.style.pageBreakBefore = origPB;
            c.style.breakBefore = origBB;
            c.style.pageBreakInside = origPI;
            c.style.breakInside = origBI;
        }});
        // Sólo aplicar avoid si la card no permite partir
        if (!allowSplit) {{
            c.style.pageBreakInside = 'avoid';
            c.style.breakInside = 'avoid';
        }}
        if (i === 0) {{ pageAccumH = h; return; }}
        const projectedH = pageAccumH + CARD_GAP_PX + h;
        if (projectedH > PAGE_USABLE_PX) {{
            c.style.pageBreakBefore = 'always';
            c.style.breakBefore = 'page';
            // Si la card permite partirse y es más alta que la página,
            // resetar accum a 0 (próxima card arrancará en página propia tras esta)
            pageAccumH = allowSplit && h > PAGE_USABLE_PX ? PAGE_USABLE_PX : h;
        }} else {{
            pageAccumH = projectedH;
        }}
    }});

    try {{
        // === Per-page-group capture con html2canvas + jsPDF ===
        // Reemplaza html2pdf().from().save() para evitar el slicing de canvas
        // que dejaba contenido pegado al borde inferior con whitespace al top.
        // La fit-to-page logic de arriba ya seteó pageBreakBefore='always' en
        // las cards que arrancan página nueva. Acá agrupamos por esos markers,
        // capturamos cada grupo (cards stackeadas + header en pág 1) como UNA
        // imagen y la centramos en la página A4. La card .pdf-allow-split
        // (Presentadas) se captura aparte y se slicéa manualmente en N páginas.

        const headerEl = document.querySelector('header');

        // Reagrupar atomicCards por pageBreakBefore markers
        const pageGroups = [];
        let currentGroup = [];
        atomicCards.forEach((c, i) => {{
            if (i === 0) {{ currentGroup = [c]; return; }}
            if (c.style.pageBreakBefore === 'always') {{
                if (currentGroup.length) pageGroups.push(currentGroup);
                currentGroup = [c];
            }} else {{
                currentGroup.push(c);
            }}
        }});
        if (currentGroup.length) pageGroups.push(currentGroup);

        // Helper: html2canvas wrapper con opciones consistentes
        const captureEl = async (el) => await html2canvas(el, {{
            scale: 2, useCORS: true, backgroundColor: '#ffffff',
            logging: false, scrollX: 0, scrollY: 0,
            windowWidth: 1000,
        }});

        // Setup PDF
        const jsPDFCtor = (window.jspdf && window.jspdf.jsPDF) || window.jsPDF;
        const pdf = new jsPDFCtor({{ unit: 'mm', format: 'a4', orientation: 'landscape' }});
        const pageW = 297, pageH = 210;
        const margin = 10;
        const usableW = pageW - 2 * margin; // 277mm
        const usableH = pageH - 2 * margin; // 190mm
        const cardGap_mm = 4;

        let isFirstPage = true;
        const startPage = () => {{
            if (!isFirstPage) pdf.addPage();
            isFirstPage = false;
        }};

        // Stack de cards (1 o más) en una página, centrado verticalmente como un bloque
        const placeStack = (canvases) => {{
            // Calcular w/h en mm de cada canvas, scaleados a usableW
            const dims = canvases.map(canvas => {{
                const w = usableW;
                const h = w * (canvas.height / canvas.width);
                return {{ canvas, w, h }};
            }});
            const totalH = dims.reduce((acc, d) => acc + d.h, 0) + cardGap_mm * (dims.length - 1);
            // Si el stack excede usableH, escalar todo proporcionalmente
            const scale = totalH > usableH ? (usableH / totalH) : 1;
            const scaledTotalH = totalH * scale;
            let y = margin + (usableH - scaledTotalH) / 2;
            dims.forEach(d => {{
                const w_mm = d.w * scale;
                const h_mm = d.h * scale;
                const x_mm = (pageW - w_mm) / 2;
                const img = d.canvas.toDataURL('image/jpeg', 0.95);
                pdf.addImage(img, 'JPEG', x_mm, y, w_mm, h_mm);
                y += h_mm + cardGap_mm * scale;
            }});
        }};

        // Slice una card alta en N páginas (top-aligned, ancho completo).
        // Cada slice usa el ALTO COMPLETO de la página A4 (usableH), así no queda
        // whitespace al fondo. Si maxPages está definido y la card es más alta que
        // maxPages * usableH, se trunca el contenido sobrante (los días más viejos
        // de la tabla quedan recortados) para respetar el cap.
        const placeSlicedCard = (canvas, maxPages) => {{
            const px_per_mm = canvas.width / usableW;
            const slice_h_px = Math.floor(usableH * px_per_mm);
            const naturalTotal = Math.ceil(canvas.height / slice_h_px);
            const total = (maxPages != null) ? Math.min(naturalTotal, maxPages) : naturalTotal;
            const effectiveH = Math.min(canvas.height, total * slice_h_px);
            for (let s = 0; s < total; s++) {{
                const y_start = s * slice_h_px;
                const y_end = Math.min(y_start + slice_h_px, effectiveH);
                const slice_h_actual = y_end - y_start;
                const sliceCanvas = document.createElement('canvas');
                sliceCanvas.width = canvas.width;
                sliceCanvas.height = slice_h_actual;
                const ctx = sliceCanvas.getContext('2d');
                ctx.fillStyle = '#ffffff';
                ctx.fillRect(0, 0, sliceCanvas.width, sliceCanvas.height);
                ctx.drawImage(canvas, 0, -y_start);
                const img = sliceCanvas.toDataURL('image/jpeg', 0.95);
                const sliceH_mm = slice_h_actual / px_per_mm;
                startPage();
                pdf.addImage(img, 'JPEG', margin, margin, usableW, sliceH_mm);
            }}
        }};

        // === Procesar cada page-group ===
        for (let g = 0; g < pageGroups.length; g++) {{
            const group = pageGroups[g];
            // Capturar cards (incluyendo header si es la primera página)
            const elementsToCapture = (g === 0 && headerEl) ? [headerEl, ...group] : group;
            const canvases = [];
            for (const el of elementsToCapture) {{
                canvases.push(await captureEl(el));
            }}
            // Detectar si en este grupo hay un .pdf-allow-split (Presentadas)
            const splitIdx = elementsToCapture.findIndex(el => el.classList && el.classList.contains('pdf-allow-split'));
            if (splitIdx >= 0) {{
                // Cards previas (si hay): emitir como stack en página propia
                if (splitIdx > 0) {{
                    startPage();
                    placeStack(canvases.slice(0, splitIdx));
                }}
                // La card a partir → ¿necesita slice?
                const card = canvases[splitIdx];
                const ratio_mm = usableW * (card.height / card.width);
                if (ratio_mm > usableH) {{
                    // Cap a 2 páginas para Presentadas: los días más viejos que no
                    // entren se truncan (igual que lo hacía el cap de 1175px previo).
                    placeSlicedCard(card, 2);
                }} else {{
                    startPage();
                    placeStack([card]);
                }}
                // Cards posteriores (no debería haber en el layout actual)
                if (splitIdx + 1 < canvases.length) {{
                    startPage();
                    placeStack(canvases.slice(splitIdx + 1));
                }}
            }} else {{
                startPage();
                placeStack(canvases);
            }}
        }}

        const fechaArchivo = new Date().toISOString().slice(0, 10);
        pdf.save('ps_verificacion_' + fechaArchivo + '.pdf');
    }} catch (err) {{
        console.error('Error al generar PDF', err);
        alert('Error al generar PDF: ' + (err && err.message ? err.message : err));
    }} finally {{
        // Restaurar todo
        restoreActions.reverse().forEach(fn => {{ try {{ fn(); }} catch (_) {{ }} }});
        if (prevTheme !== 'light') {{
            document.documentElement.setAttribute('data-theme', prevTheme);
        }}
        if (typeof recomputeKPIsAndCharts === 'function') recomputeKPIsAndCharts();
        overlay.remove();
        pdfBtn.disabled = false;
        pdfBtn.innerHTML = labelOriginal;
    }}
}}
</script>
</body>
</html>"""
    return html


def load_previous_verifications(csv_path: str) -> dict:
    """Carga verificaciones DGR previas desde un CSV existente.
    Retorna dict de (cuit_str, fecha_str) -> {
        'verificada_dgr': str, 'jurisdicciones': str, 'jurisdicciones_check': str
    }.
    Cada columna se reutiliza de forma independiente: si el CSV previo
    tiene 'verificada_dgr' válida pero no tiene 'jurisdicciones' (o tiene
    error), se reutiliza sólo la DDJJ y el padrón se reverifica.
    'jurisdicciones_check' se arrastra tal cual; `verificar_en_dgr` decide
    si refrescar padrón según el TTL."""
    p = Path(csv_path)
    if not p.exists():
        return {}

    df_prev = pd.read_csv(csv_path, encoding="utf-8-sig")
    if "verificada_dgr" not in df_prev.columns:
        return {}

    df_prev["verificada_dgr"] = df_prev["verificada_dgr"].fillna("")
    has_jur = "jurisdicciones" in df_prev.columns
    if has_jur:
        df_prev["jurisdicciones"] = df_prev["jurisdicciones"].fillna("").astype(str)
    has_check = "jurisdicciones_check" in df_prev.columns
    if has_check:
        df_prev["jurisdicciones_check"] = df_prev["jurisdicciones_check"].fillna("").astype(str)

    error_states = {"", "Error", "Error login"}
    base_mask = df_prev["duplicado"] == "No"

    result = {}
    for _, row in df_prev[base_mask].iterrows():
        key = (str(row["cuit"]), str(row["fecha"]))
        v_dgr = row["verificada_dgr"]
        v_jur = row["jurisdicciones"] if has_jur else ""
        v_chk = row["jurisdicciones_check"] if has_check else ""

        # Sólo reutilizar cada columna si NO está en estado de error.
        # Si una está mal, se deja vacía y se re-consulta en la corrida nueva.
        carry_dgr = v_dgr if v_dgr not in error_states else ""
        carry_jur = v_jur if v_jur not in error_states else ""
        # Si jur es inválido, el check tampoco se reutiliza (forzará refresco)
        carry_chk = v_chk if carry_jur else ""

        if carry_dgr == "" and carry_jur == "":
            continue  # nada para reutilizar en esta fila

        result[key] = {
            "verificada_dgr": carry_dgr,
            "jurisdicciones": carry_jur,
            "jurisdicciones_check": carry_chk,
        }

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
        for col in ["estrellas_valor", "texto_feedback", "texto_del_error"]:
            if col in df.columns:
                df[col] = df[col].fillna("")
        if "texto_del_error" not in df.columns:
            df["texto_del_error"] = ""
        if "jurisdicciones" not in df.columns:
            df["jurisdicciones"] = ""
        if "jurisdicciones_check" not in df.columns:
            df["jurisdicciones_check"] = ""

        con_dgr = "verificada_dgr" in df.columns

        # Cargar CSV de errores si existe (paralelo al CSV principal)
        err_csv_path = args.desde_csv.replace(".csv", "_errores.csv")
        if Path(err_csv_path).exists():
            df_err = pd.read_csv(err_csv_path, encoding="utf-8-sig")
            for col in ["region", "total", "texto_del_error"]:
                if col in df_err.columns:
                    df_err[col] = df_err[col].fillna("")
            print(f"  📊 Errores cargados desde {err_csv_path} ({len(df_err)} filas)")
        else:
            df_err = pd.DataFrame(columns=["cuit", "exact_timestamp", "region", "total", "texto_del_error", "numero_eventos"])
            print(f"  ℹ️  No se encontró {err_csv_path}; tab de errores quedará vacía")

        # Cargar chart_data desde JSON si existe (tiene series de GA4)
        chart_json_path = args.desde_csv.replace(".csv", "_charts.json")
        if Path(chart_json_path).exists():
            with open(chart_json_path, "r", encoding="utf-8") as f:
                chart_data = json.load(f)
            print(f"  📊 Datos de gráficos cargados desde {chart_json_path}")
        else:
            chart_data = _build_chart_data_from_df(df)

        print(f"📝 Generando reporte → {args.output}")
        html = generate_report(df, df_err, args.desde, args.hasta, con_dgr, chart_data)

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
    df, df_err, chart_data = extract_ga4_data(args.credentials, args.desde, args.hasta)
    if df.empty:
        print("\n❌ Sin datos. Verificá el rango de fechas y los permisos.")
        sys.exit(1)

    # ── Early-exit: en modo incremental, si no hay eventos nuevos respecto al CSV
    # previo (presentaciones NI errores), salimos sin tocar DGR ni regenerar el reporte. ──
    if args.incremental:
        prev_csv_path = Path(args.output.replace(".html", ".csv"))
        prev_err_csv_path = Path(args.output.replace(".html", "_errores.csv"))
        if prev_csv_path.exists():
            try:
                prev_df = pd.read_csv(prev_csv_path, encoding="utf-8-sig", dtype=str)
                sig_cols = ["cuit", "exact_timestamp", "nombre_evento"]
                pres_nuevos = None
                if all(c in prev_df.columns for c in sig_cols):
                    prev_sig = set(map(tuple, prev_df[sig_cols].astype(str).values))
                    new_sig = set(map(tuple, df[sig_cols].astype(str).values))
                    pres_nuevos = new_sig - prev_sig

                # Errores: comparar (cuit, exact_timestamp) si hay archivo previo
                err_nuevos = None
                err_sig_cols = ["cuit", "exact_timestamp"]
                if prev_err_csv_path.exists():
                    try:
                        prev_err_df = pd.read_csv(prev_err_csv_path, encoding="utf-8-sig", dtype=str)
                        if all(c in prev_err_df.columns for c in err_sig_cols):
                            prev_err_sig = set(map(tuple, prev_err_df[err_sig_cols].astype(str).values))
                            new_err_sig = set(map(tuple, df_err[err_sig_cols].astype(str).values)) if not df_err.empty else set()
                            err_nuevos = new_err_sig - prev_err_sig
                    except Exception as exc:
                        print(f"  ⚠️  No se pudo comparar errores con CSV previo ({exc}).")
                else:
                    # No hay archivo previo de errores; si hay errores nuevos, hay que regenerar
                    err_nuevos = set(range(len(df_err))) if not df_err.empty else set()

                if pres_nuevos is not None and not pres_nuevos and (err_nuevos is None or not err_nuevos):
                    total_pres = len(set(map(tuple, df[sig_cols].astype(str).values)))
                    total_err = len(df_err)
                    print(
                        f"\n✅ Sin eventos nuevos en GA4 "
                        f"({total_pres} presentaciones + {total_err} errores, todo ya procesado)."
                    )
                    print("   Saltando verificación DGR y regeneración de reporte.\n")
                    print(f"{'═' * 60}\n")
                    return
                msgs = []
                if pres_nuevos:
                    msgs.append(f"{len(pres_nuevos)} presentaciones nuevas")
                if err_nuevos:
                    msgs.append(f"{len(err_nuevos)} errores nuevos")
                if msgs:
                    print(f"\n📌 Detectados: {', '.join(msgs)} desde la última corrida.")
            except Exception as exc:
                print(f"  ⚠️  No se pudo comparar con CSV previo ({exc}). Continuando con flujo completo.")

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
    html = generate_report(df, df_err, args.desde, args.hasta, con_dgr, chart_data)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)

    # Guardar CSV principal, CSV de errores y chart_data JSON
    csv_path = args.output.replace(".html", ".csv")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    err_csv_path = args.output.replace(".html", "_errores.csv")
    df_err.to_csv(err_csv_path, index=False, encoding="utf-8-sig")

    chart_json_path = args.output.replace(".html", "_charts.json")
    if chart_data:
        with open(chart_json_path, "w", encoding="utf-8") as f:
            json.dump(chart_data, f, ensure_ascii=False)

    print(f"\n{'═' * 60}")
    print(f"  ✨ Listo!")
    print(f"  📊 Reporte: {args.output}")
    print(f"  📄 CSV:     {csv_path}")
    print(f"  📄 Errores: {err_csv_path}")
    print(f"{'═' * 60}\n")


if __name__ == "__main__":
    main()
