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
- **Diferencia column styling**: `.dif-pos` and `.dif-neg` now render with `font-weight: 600` to match the rest of the bar-table values. `.dif-pos` color changed from `--text-dim` (gris) to `--text` (blanco del tema); `.dif-neg` stays red
- **Fixed Timestamp column sort**: The header sort handler used `parseFloat(text)` to detect numeric columns, which for ISO timestamps like `2026-04-08 12:34:56` returned `2026` (just the year prefix). All rows compared as the same number, so clicking Timestamp appeared to do nothing. Switched to `Number()` (requires the full string to be numeric) plus an empty-string guard so only truly numeric columns take the numeric branch

### Session 2026-04-14
- **Added "Jurisdicciones" column (padrón ARCA/BC)**: New column **inserted at position 4** between Región and Total (only shown when `con_dgr=True`) listing the jurisdiction codes associated with each CUIT, from the DGR Padrón Web (ARCA/BlockChain) module. Values are rendered as comma-separated codes, e.g. `"904, 921"` or `"902, 910, 917"`. Data source is `pwContribBlockChain.do?method=buscar&cuit=<CUIT>` — a DIFFERENT DGR module from SIFERE WEB (which `dgr_search_cuit` uses). With DGR enabled the "Sin duplicados" column order is: CUIT(0), Timestamp(1), Evento(2), Región(3), **Jurisdicciones(4)**, Total(5), Estrellas(6), Feedback(7), Texto del Error(8), Duplicado(9), Verificada DGR(10). The "Con duplicados" tab does not include Jurisdicciones or DGR columns, so its indices stay unchanged (Total=4, Estrellas=5, Feedback=6, etc.)
- **Dynamic column headers + JS cell indices**: Replaced the hardcoded `<th data-col="N">` headers and `cells[N]` JS references with a Python-side `_build_th_rows(cols)` helper that generates header/filter rows from a list of column names, plus `js_idx_*` variables that inject the correct `cells[N]` into the inline JS based on `con_dgr`. Avoids double-maintaining indices when future columns are inserted. The only hardcoded reference left is `cells[1]` (Timestamp), which is the same in both tables regardless of `con_dgr`
- **New constant and endpoints**: Added `DGR_PADRON_URL = f"{DGR_BASE}/pwContribBlockChain.do"`. Init endpoint is `?method=buscarInBc` (same pattern as DDJJ's `buscarCuitIn`), search endpoint is `?method=buscar&cuit=<CUIT>`
- **`DGRJurParser` (HTMLParser)**: New parser that extracts jurisdiction codes from the `<div id="jur">` tab of the padrón response. Tracks div depth to handle nested divs cleanly. Only reads the FIRST `<tbody>` inside `#jur` to avoid bleeding into subsequent tabs (`rel`, `documento`, `telemail`). Extracts the numeric prefix before `-` in the 3rd `<td>` (e.g. `"910-JUJUY"` → `"910"`). Requires >= 6 `<td>` cells to filter out "NO TIENE ..." colspan rows. Exposes `.codes_str` (comma-joined) and `.count` (len) properties
- **Two-pass DGR verification (breaking internal change)**: Refactored `verificar_en_dgr` to run DDJJ and Padrón queries in SEPARATE sequential passes, each with its own init call (`dgr_init_search_form` for DDJJ, `dgr_init_padron_web` for Padrón). Previously tried to interleave them which caused Struts session collection collisions (calling `pwContribBlockChain.do` between DDJJ searches reset the SIFERE collections). Each pass uses its own independent `cuits_need_*` mask so a CUIT only verified in one CSV (e.g. from an old run without `jurisdicciones`) still gets the missing column filled
- **Fixed DGR login: Tomcat URL rewriting**: `dgr_login` was POSTing to `/j_security_check` without the `;jsessionid=...` suffix that Tomcat embeds in the form's `action` attribute. Server was rebounding to `login.jsp?error=true` even with correct credentials. Fix: parse the `<form action="...">` from the GET of `login.jsp` and POST to that exact URL. Also added `Referer`/`Origin` headers and the `login=Entrar` form field that the real browser submit sends
- **`load_previous_verifications` returns dict-of-dict**: Changed return type from `dict[(cuit,fecha)] -> str` to `dict[(cuit,fecha)] -> {'verificada_dgr': str, 'jurisdicciones': str}` so incremental runs can carry over both columns. CSV files without a `jurisdicciones` column return `{}` (empty), forcing full reverification once — avoids partial carry-over where DDJJ is reused but padrón would skip
- **`--desde-csv` backward compat**: When loading older CSVs without `jurisdicciones`, the column is created empty so the report still renders (same pattern as `texto_del_error`)
- **`.gitignore` extended**: Added `debug_padron_*.html` (new debug artifact), `test_*.{html,csv,json}`, and `ps_verificacion_test.*` patterns

### Session 2026-04-28 — PDF download rebuild

Rework completo del flow de descarga PDF (`generarPDF()` JS al final del `<script>` dentro de `generate_report()`). El approach cambió de A4 portrait + container 760px a **A4 landscape + container 1000px**, con lógica de paginado custom basada en altura de cards.

- **Bug raíz del corte horizontal**: `windowWidth: 760` en `html2canvas` opts hacía que el contenido se compactara en la mitad izquierda del canvas (con la mitad derecha vacía). Removerlo y dejar sólo `width: 1000` produce un canvas correcto del container al ancho real
- **Layout PDF**: A4 landscape (`format: 'a4', orientation: 'landscape'`), margins `[10, 10, 12, 10]` mm → área útil ≈ 277×188mm = 1047×711px @ 96dpi. Container forzado a 1000px durante PDF gen, con padding/margin a 0 sobre `.container` y `body`
- **Lógica fit-to-page** (post-charts render): cards consecutivas se acumulan en una página mientras `(suma + CARD_GAP_PX=24) ≤ PAGE_USABLE_PX=620` (buffer de 91px sobre 711 para gaps/paddings/imprecisión). Al exceder, `pageBreakBefore: 'always'` sobre la card que no entra y se resetea el accum. Incluye `.kpis` en la lista para que los bloques de KPIs cuenten en el cálculo. Cards consecutivas en grid horizontal (mismo y, parent display: grid/flex) se manejan según corresponda — en ps_verificacion no hay grids horizontales reales (charts-grid stackea vertical a 1000px); en ps_flujo el grid Dispositivo/SO/Browser SÍ se detecta y se fuerza vertical
- **`.pdf-allow-split` para tabla larga**: la card "Presentadas (sin duplicados)..." tiene una `bar-table-wrap` que se quiere mostrar a través de 2 páginas. Se le agrega clase `.pdf-allow-split`, el CSS inyectado la excluye de `page-break-inside: avoid`, padding/margin a 0, title compactado (`font 0.72rem, padding 0.2/0.4`), `bar-table-wrap` con `max-height: 1175px` (cap manual del usuario; cualquier día que iría a página 6 se recorta con `overflow: hidden`)
- **Reorden DOM**: la card "Encuesta valoraciones" se mueve antes de "Presentadas..." en su parent `.charts-grid` para que quede en la página de KPIs2. Detección estricta con `startsWith('encuesta:')` o `includes('valoraciones')` porque el título de "Presentadas..." también contiene la palabra "Encuestas"
- **Cap selectivo de `bar-table-wrap`**: 410px para tablas chicas (Errores de validación), 1175px para Presentadas. Comparado con cap=520px del CSS original
- **Compactación CSS de tablas regulares (no `.bar-table`)**: `font-size: 0.78rem`, `td/th padding: 0.3rem 0.6rem`, `line-height: 1.35`. Y `.card { padding: 1rem }` (antes 1.5rem). Reduce ~24% la altura de tablas y permite agrupar más cards por página
- **Wrap natural en headers**: `table:not(.bar-table) th, td { white-space: normal }` (sin `word-break: break-word` que partía palabras carácter por carácter en columnas angostas)
- **Cards "fantasma" al final**: un paso oculta `.card` direct children del `.container` que tengan `top >= maxBottom` (después del último elemento real con `h>50`) Y `(height < 80 || textContent.length < 5)`. Filtra los placeholders chicos que aparecían como cuadritos vacíos en una página adicional. La condición `top >= maxBottom` es crítica para no ocultar separadores estructurales del medio del documento
- **Layout final ps_verificacion (5 páginas)**:
    - Pág 1: Header + KPIs1 + Errores de validación por día
    - Pág 2: Top textos de error
    - Pág 3: KPIs2 + Encuesta valoraciones (estrellas + doughnut + word cloud)
    - Pág 4-5: Presentadas/Encuestas (capped, ~1175px de wrap, los días anteriores se recortan)
- **Footer del informe oculto** en el PDF (`.container > footer, body > footer` agregados a hideSelectors)
- **Limitación conocida**: la lógica fit-to-page se basa en altura medida en el DOM live, pero el render del PDF puede diferir levemente. Por eso el threshold es conservador (620 en vez de 711) y el cap del wrap de Presentadas necesitó iterarse a mano (1100 → 1150 → 1175)
- **Diagnóstico vía preview server**: `python -m http.server 8765` en el repo + previewer mcp (`mcp__Claude_Preview__*`). El truco para ver canvas intermedio es pintarlo a `<canvas>` dentro de la página, que `preview_screenshot` puede capturar (renderear el PDF en `<iframe>` no funciona — el visor lo maneja la app del browser host). Para inspeccionar layout post-prep replicar manualmente los pasos del `generarPDF()` en `preview_eval`
