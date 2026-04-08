# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Pipeline de verificacion cruzada GA4 + DGR Gestion para COMARB (organismo tributario argentino). Extrae eventos de presentacion simplificada de GA4, deduplica por CUIT+fecha, cruza datos de encuesta, verifica contra el sistema interno DGR Gestion, y genera reportes HTML/CSV.

## Commands

```bash
# Install dependencies
pip install -r requirements_ps.txt

# Run with DGR verification
python ps_verificacion.py -c comarb-analytics-580ca8f5412c.json -u USER_DGR -p PASS_DGR

# Run GA4 only (no DGR)
python ps_verificacion.py -c comarb-analytics-580ca8f5412c.json --solo-ga4

# Custom date range
python ps_verificacion.py -c comarb-analytics-580ca8f5412c.json -u USER -p PASS --desde 2026-01-01 --hasta 2026-03-31

# Debug mode (saves raw DGR HTML for a specific CUIT)
python ps_verificacion.py -c comarb-analytics-580ca8f5412c.json -u USER -p PASS --debug CUIT_NUMBER
```

## Architecture

Single-file script (`ps_verificacion.py`) with 4 sequential steps:

1. **GA4 Extraction** (`extract_ga4_data`): Two queries to GA4 Data API v1beta:
   - Query 1: Presentation events (`PS_boton_presentar_y_salir`, `PS_boton_presentar_y_generar_pago`) with dimensions CUIT, exact_timestamp, eventName, region, Total, texto_del_error
   - Query 2: Survey event (`PS_boton_enviar_encuesta`) with dimensions CUIT, exact_timestamp, estrellas_valor, texto_feedback
   - Results are merged by CUIT + date (survey data is optional, many sessions won't have it)

2. **Deduplication** (`deduplicate`): PARTITION BY cuit + date, ORDER BY timestamp DESC. Row 1 = "No" (kept), rest = "Si" (duplicate)

3. **DGR Verification** (`verificar_en_dgr`): For each non-duplicate CUIT:
   - Login via POST to j_security_check
   - **Must call** `dgr_init_search_form` (GET `buscarCuitIn`) after login to initialize Struts session collections before any search
   - Search with anticipo range extended 3 months before start_date (anticipos from earlier periods can be presented within the date range)
   - Cross-verify by matching GA4 event date against `fecha_presentacion_afip` in DGR, then check if that specific DDJJ has "(S)" in the Formulario column

4. **Report Generation** (`generate_report`): HTML with dark theme, KPIs, two tabbed tables (with/without duplicates), column filters, sortable headers. Also outputs CSV.

## Key Technical Details

- **GA4 Property ID**: 485388348 ("COMARB - Sifere Web - Presentacion Simplificada")
- **GA4 dimension names**: Use actual parameter names (`customEvent:CUIT`, `customEvent:exact_timestamp`, `customEvent:Total`), NOT numbered indices (`customParamDimension1`)
- **DGR Struts requirement**: The DGR system uses Apache Struts. You MUST GET `sfrwDdjj.do?method=buscarCuitIn` before searching, otherwise the server returns a JSP error (`Failed to obtain specified collection`)
- **Windows encoding**: stdout/stderr are forced to UTF-8 at script startup because Windows cp1252 can't handle Unicode box-drawing characters and emojis used in console output
- **DGR search anticipo range**: Extended 3 months before --desde to cover cases where earlier period anticipos (e.g., 202512) are presented within the date range (e.g., January 2026)

## Session Changelog

### Session 2026-04-01 / 2026-04-02
- **Fixed GA4 dimension names**: Changed `customEvent:customParamDimension1/3/4` to `customEvent:CUIT/Total/exact_timestamp` (GA4 Data API uses actual parameter names, not UA-style numbered indices)
- **Fixed Windows encoding**: Added UTF-8 forcing for stdout/stderr to handle Unicode characters on Windows cp1252 consoles
- **Fixed DGR verification (Struts init)**: Added `dgr_init_search_form()` call after login to initialize Struts session. Without this, all DGR searches returned JSP errors
- **Fixed DGR search parameters**: Added full filter params (estado, flgTipoPeriodo, anticipo range, fecha range) to match what DGR expects. Extended anticipo range 3 months back
- **Fixed DGR date matching**: Changed verification from "any DDJJ with (S)" to matching GA4 event date against `fecha_presentacion_afip` before checking for "(S)". This fixed false negatives where a CUIT had (S) for one period but not another
- **Added --debug mode**: Saves raw DGR HTML response for a specific CUIT for troubleshooting
- **Added survey data**: Second GA4 query for `PS_boton_enviar_encuesta` event, merged by CUIT+date into main DataFrame (estrellas_valor, texto_feedback columns)
- **HTML improvements**: Added column filters (text input per column), sortable headers (click to toggle asc/desc, Timestamp defaults to desc), removed event count and "Verificadas DGR Si" KPIs, reordered columns (N Eventos to 3rd position), renamed events to readable labels ("Presentar y Salir" / "Presentar y Generar Pago")

### Session 2026-04-08
- **Replaced "Nº Eventos" column with "Texto del Error"**: Removed the event count column from both report tables (con/sin duplicados) and added a new column showing the `customEvent:texto_del_error` GA4 parameter. `numero_eventos` is still extracted internally because it feeds the daily presentations chart, but it's no longer rendered in the HTML tables. Column indices were re-assigned so Estrellas/Feedback/Duplicado/DGR stay at positions 5/6/7/8/9 (preserved JS references for KPI recalculation)
- **GA4 dimension `customEvent:texto_del_error`**: Registered in the GA4 property but currently returns `(not set)` for all events because the frontend parameter was created less than 24h before this change. Cleaned to empty string at extraction time. Column will populate automatically once the frontend starts emitting values
- **`--desde-csv` backward compat**: When loading older CSVs without `texto_del_error`, the column is created empty so the report still renders
- **Fixed empty Estrellas/Feedback charts**: `recomputeKPIsAndCharts` was reading `cells[6]` (Estrellas) and `cells[7]` (Feedback) — those were the old positions before the `texto_del_error` change. After the column rework Estrellas moved to `cells[5]` and Feedback to `cells[6]`, so the charts derived from `tbl-no-dup` always saw empty/wrong data. Updated the JS indices. Other JS cell references (`cells[8]` Duplicado, `cells[9]` DGR) were already correct
- **Diferencia formula flipped**: The "Diferencia" column in the daily presentations table now computes `presentadas_no_dup - (enc_enviadas + enc_cerradas)` (previously it was `(enc_enviadas + enc_cerradas) - presentadas_no_dup`). Updated both the Python `diferencia` array and `renderBarTable` JS. Sign convention now: positive = more presentaciones than encuestas (faltaron encuestas), negative = más encuestas que presentaciones
