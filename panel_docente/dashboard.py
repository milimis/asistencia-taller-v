import streamlit as st
import pandas as pd
import gspread
import json
import os
import requests
import hashlib
import time
from pathlib import Path
from google.oauth2.service_account import Credentials

# ── Configuración ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Taller V · Panel Docente",
    page_icon="🎓",
    layout="wide"
)

# ── Login ──────────────────────────────────────────────────────────────────────
def _hacer_token(usuario: str) -> str:
    """Token válido por 8 horas basado en el bloque horario actual."""
    bloque = int(time.time()) // (8 * 3600)
    return hashlib.sha256(f"{usuario}:taller_v:{bloque}".encode()).hexdigest()[:20]

def check_login():
    # Restaurar sesión desde query param en la URL
    if not st.session_state.get("logged_in"):
        token = st.query_params.get("t", "")
        if token:
            usuarios = dict(st.secrets.get("users", {}))
            for u in usuarios:
                if token == _hacer_token(u):
                    st.session_state.logged_in = True
                    st.session_state.usuario = u
                    break

    if st.session_state.get("logged_in"):
        return True

    st.markdown("""
        <div style='max-width:380px;margin:80px auto 0;'>
        <h2 style='text-align:center;margin-bottom:4px'>🎓 Taller V</h2>
        <p style='text-align:center;color:#888;margin-bottom:32px'>Panel docente · FADU 2026</p>
        </div>
    """, unsafe_allow_html=True)

    with st.form("login_form"):
        col_l, col_c, col_r = st.columns([1, 2, 1])
        with col_c:
            usuario = st.text_input("Usuario")
            password = st.text_input("Contraseña", type="password")
            ok = st.form_submit_button("Ingresar", use_container_width=True, type="primary")

    if ok:
        usuarios = dict(st.secrets.get("users", {}))
        if usuario in usuarios and usuarios[usuario] == password:
            st.session_state.logged_in = True
            st.session_state.usuario = usuario
            st.query_params["t"] = _hacer_token(usuario)
            st.rerun()
        else:
            st.error("Usuario o contraseña incorrectos.")

    return False

if not check_login():
    st.stop()

# ── Constantes ────────────────────────────────────────────────────────────────
SHEET_ID     = "1MtXr_FekHJIZiE-Cjxc3PwalNbZtqvP8O3GBJCMWDi4"
SHEET_NAME   = "Notas"
API_URL      = "https://asistencia-taller-v-production.up.railway.app"
TOTAL_CLASES = 32
EXCEL        = Path(__file__).parent.parent / "backend/data/planilla.xlsx"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

CAMPOS_NOTAS = ["esquema", "viz_datos", "final_m1", "relev", "individual", "final_m2", "bit1", "bit2", "bit3"]

MODULOS = {
    "M1": {
        "label": "Módulo 1", "total": 50, "color": "#1a6b8a",
        "items": {
            "esquema":   {"label": "Esquema",    "max": 10, "fecha": "14 abr"},
            "viz_datos": {"label": "Viz. datos", "max": 15, "fecha": "23 abr"},
            "final_m1":  {"label": "Final M1",   "max": 25, "fecha": "19 may"},
        }
    },
    "M2": {
        "label": "Módulo 2", "total": 44, "color": "#2e6b4f",
        "items": {
            "relev":      {"label": "Relevamiento", "max": 9,  "fecha": "2 jun"},
            "individual": {"label": "Individual",   "max": 13, "fecha": "16 jun"},
            "final_m2":   {"label": "Final M2",     "max": 22, "fecha": "7 jul"},
        }
    },
    "BIT": {
        "label": "Bitácoras", "total": 6, "color": "#8b6914",
        "items": {
            "bit1": {"label": "Bitácora 1", "max": 2, "fecha": "16 abr"},
            "bit2": {"label": "Bitácora 2", "max": 2, "fecha": "11 jun"},
            "bit3": {"label": "Bitácora 3", "max": 2, "fecha": "3 jul"},
        }
    }
}

HEADERS = ["key", "apellido", "nombre", "equipo"] + CAMPOS_NOTAS

# ── Helpers ───────────────────────────────────────────────────────────────────
def categoria_fadu(pct):
    if pct >= 87.5: return "Excelente",       "#1e8449"
    if pct >= 75.0: return "Muy bueno",        "#27ae60"
    if pct >= 62.5: return "Bueno",            "#d4ac0d"
    if pct >= 50.0: return "Aceptable",        "#e67e22"
    if pct >= 25.0: return "Insuficiente",     "#cb4335"
    return              "Muy insuficiente",    "#922b21"

def badge(texto, color):
    return f'<span style="background:{color};color:white;padding:3px 10px;border-radius:12px;font-size:0.85em;font-weight:600">{texto}</span>'

def barra(valor, maximo, color="#1a6b8a"):
    pct = (valor / maximo * 100) if maximo > 0 else 0
    return f"""
    <div style="background:#e9ecef;border-radius:6px;height:10px;width:100%">
      <div style="background:{color};width:{pct}%;height:10px;border-radius:6px"></div>
    </div>
    <small style="color:#666">{valor}/{maximo} ({pct:.0f}%)</small>
    """

def calcular_totales(nota_est):
    m1  = sum(nota_est.get(k, 0) for k in MODULOS["M1"]["items"])
    m2  = sum(nota_est.get(k, 0) for k in MODULOS["M2"]["items"])
    bit = sum(nota_est.get(k, 0) for k in MODULOS["BIT"]["items"])
    return m1, m2, bit, m1 + m2 + bit

def normalizar(s):
    import unicodedata
    s = s.lower().strip()
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

# ── Google Sheets ─────────────────────────────────────────────────────────────
@st.cache_resource
def get_sheet():
    creds_dict = None

    # 1. Variable de entorno (desarrollo / Render)
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if creds_json:
        creds_dict = json.loads(creds_json)

    # 2. st.secrets como tabla TOML [gcp_service_account]
    if creds_dict is None and "gcp_service_account" in st.secrets:
        creds_dict = dict(st.secrets["gcp_service_account"])

    # 3. st.secrets como string JSON
    if creds_dict is None and "GOOGLE_SERVICE_ACCOUNT_JSON" in st.secrets:
        creds_dict = json.loads(st.secrets["GOOGLE_SERVICE_ACCOUNT_JSON"])

    # 4. Archivo local para desarrollo
    if creds_dict is None:
        local_creds = Path(__file__).parent / "credentials_local.json"
        if local_creds.exists():
            creds_dict = json.loads(local_creds.read_text())

    if creds_dict is None:
        st.error("❌ Credenciales de Google no encontradas. Configurá los Secrets en Streamlit Cloud.")
        st.stop()

    credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client      = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(SHEET_ID)
    try:
        sheet = spreadsheet.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(SHEET_NAME, rows=200, cols=20)
    return sheet

def init_sheet_if_empty(sheet, estudiantes):
    """Inicializa el sheet con headers y estudiantes si está vacío."""
    data = sheet.get_all_values()
    if data and any(cell for row in data for cell in row):
        return  # Ya inicializado con contenido real
    rows = [HEADERS]
    for est in estudiantes:
        rows.append([
            est["key"], est["apellido"], est["nombre"], est["equipo"],
            0, 0, 0, 0, 0, 0, 0, 0, 0
        ])
    sheet.update("A1", rows)

@st.cache_data(ttl=30)
def load_notas_sheet():
    sheet = get_sheet()
    data  = sheet.get_all_records()
    notas = {}
    for row in data:
        key = row.get("key", "")
        if key:
            notas[key] = {c: float(row.get(c, 0) or 0) for c in CAMPOS_NOTAS}
    return notas

def save_nota_sheet(est, nota_est):
    sheet = get_sheet()
    data  = sheet.get_all_values()
    headers = data[0]
    key_col = headers.index("key") + 1
    for i, row in enumerate(data[1:], start=2):
        if row[key_col - 1] == est["key"]:
            for campo in CAMPOS_NOTAS:
                col = headers.index(campo) + 1
                sheet.update_cell(i, col, nota_est.get(campo, 0))
            return
    # Si no existe, agregar fila nueva
    nueva_fila = [est["key"], est["apellido"], est["nombre"], est["equipo"]] + \
                 [nota_est.get(c, 0) for c in CAMPOS_NOTAS]
    sheet.append_row(nueva_fila)

# ── Carga de estudiantes ──────────────────────────────────────────────────────
@st.cache_data
def load_students():
    xl = pd.ExcelFile(EXCEL)
    df = xl.parse("Notas", header=None)
    estudiantes = []
    for _, row in df.iloc[4:].iterrows():
        if pd.notna(row[1]) and pd.notna(row[2]):
            apellido = str(row[1]).strip().rstrip(",")
            nombre   = str(row[2]).strip().rstrip(",")
            equipo   = str(row[3]).strip() if pd.notna(row[3]) else ""
            num      = int(row[0]) if pd.notna(row[0]) else 0
            estudiantes.append({
                "num": num, "apellido": apellido,
                "nombre": nombre, "equipo": equipo,
                "key": f"{apellido}_{nombre}"
            })
    return estudiantes

# ── Asistencia desde API ──────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def load_asistencia():
    # Primero intentar CSV local
    csv_path = Path(__file__).parent.parent / "backend/data/asistencia.csv"
    if csv_path.exists():
        df = pd.read_csv(csv_path)
        resultado = {}
        for email, group in df.groupby("email"):
            apellido = str(group["apellido"].iloc[0]).strip()
            nombre   = str(group["nombre"].iloc[0]).strip()
            resultado[normalizar(apellido.split()[0])] = {
                "apellido": apellido, "nombre": nombre,
                "clases": len(group["fecha"].unique())
            }
        return resultado
    # Fallback: API remota
    try:
        r = requests.get(f"{API_URL}/attendance", timeout=10)
        if r.status_code == 200:
            data = r.json().get("attendance", [])
            resultado = {}
            for reg in data:
                apellido = str(reg.get("apellido", "")).strip()
                nombre   = str(reg.get("nombre", "")).strip()
                fecha    = reg.get("fecha", "")
                key      = normalizar(apellido.split()[0])
                if key not in resultado:
                    resultado[key] = {"apellido": apellido, "nombre": nombre, "fechas": set()}
                resultado[key]["fechas"].add(fecha)
            return {k: {**v, "clases": len(v["fechas"])} for k, v in resultado.items()}
    except Exception:
        pass
    return {}

def buscar_asistencia(est, asist):
    ap = normalizar(est["apellido"].split()[0])
    if isinstance(asist, dict):
        # Formato API o fallback CSV
        for k, v in asist.items():
            k_norm = normalizar(str(k).split()[0]) if isinstance(k, str) else ""
            ap_v   = normalizar(str(v.get("apellido","")).split()[0])
            if ap in ap_v or ap_v in ap:
                return v.get("clases", 0)
    return 0

# ── Inicialización ────────────────────────────────────────────────────────────
estudiantes = load_students()
sheet       = get_sheet()
init_sheet_if_empty(sheet, estudiantes)
notas       = load_notas_sheet()
asistencia  = load_asistencia()

# ── Sidebar ───────────────────────────────────────────────────────────────────
st.sidebar.title("🎓 Taller V · 2026")
st.sidebar.caption("Panel docente — Vespertino")
st.sidebar.divider()
vista = st.sidebar.radio(
    "Navegación",
    ["📊 Resumen general", "👤 Por estudiante", "✏️ Ingresar notas", "📋 Pasar lista", "📓 Bitácoras"],
    label_visibility="collapsed"
)
st.sidebar.divider()
st.sidebar.caption(f"👥 {len(estudiantes)} estudiantes · 23 equipos")
st.sidebar.caption(f"📅 {TOTAL_CLASES} clases · mín. 85%")
st.sidebar.divider()
st.sidebar.caption(f"👤 {st.session_state.get('usuario', '')}")
if st.sidebar.button("Cerrar sesión"):
    st.query_params.clear()
    st.session_state.logged_in = False
    st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# VISTA 1 — RESUMEN GENERAL
# ══════════════════════════════════════════════════════════════════════════════
if vista == "📊 Resumen general":
    st.title("📊 Resumen general")

    filas = []
    for est in estudiantes:
        nota_est = notas.get(est["key"], {})
        m1, m2, bit, total = calcular_totales(nota_est)
        cat, col  = categoria_fadu(total)
        clases    = buscar_asistencia(est, asistencia)
        pct_asist = (clases / TOTAL_CLASES) * 100

        filas.append({
            "N°":         est["num"],
            "Apellido":   est["apellido"],
            "Nombre":     est["nombre"],
            "Equipo":     est["equipo"],
            "M1 /50":     m1,
            "M2 /44":     m2,
            "Bit /6":     bit,
            "Total /100": total,
            "Categoría":  cat,
            "Clases":     clases,
            "% Asist.":   f"{pct_asist:.0f}%",
            "Asist.":     "✅" if pct_asist >= 85 else ("⚠️" if pct_asist >= 70 else "❌"),
        })

    df = pd.DataFrame(filas)

    c1, c2, c3, c4 = st.columns(4)
    con_notas = sum(1 for f in filas if f["Total /100"] > 0)
    aprobados = sum(1 for f in filas if f["Total /100"] >= 50)
    asist_ok  = sum(1 for f in filas if int(f["% Asist."].replace("%","")) >= 85)
    c1.metric("Estudiantes", len(filas))
    c2.metric("Con notas cargadas", con_notas)
    c3.metric("Aprueban (≥50%)", aprobados)
    c4.metric("Asistencia OK (≥85%)", asist_ok)

    st.divider()

    col_f1, col_f2, col_f3 = st.columns(3)
    filtro_eq    = col_f1.selectbox("Equipo", ["Todos"] + sorted(df["Equipo"].unique().tolist()))
    filtro_cat   = col_f2.selectbox("Categoría", ["Todas", "Excelente", "Muy bueno", "Bueno", "Aceptable", "Insuficiente", "Muy insuficiente"])
    filtro_asist = col_f3.selectbox("Asistencia", ["Todas", "✅ OK (≥85%)", "⚠️ Riesgo", "❌ Crítica"])

    df_f = df.copy()
    if filtro_eq    != "Todos":   df_f = df_f[df_f["Equipo"]    == filtro_eq]
    if filtro_cat   != "Todas":   df_f = df_f[df_f["Categoría"] == filtro_cat]
    if filtro_asist == "✅ OK (≥85%)": df_f = df_f[df_f["Asist."] == "✅"]
    elif filtro_asist == "⚠️ Riesgo":  df_f = df_f[df_f["Asist."] == "⚠️"]
    elif filtro_asist == "❌ Crítica": df_f = df_f[df_f["Asist."] == "❌"]

    st.dataframe(
        df_f, use_container_width=True, hide_index=True,
        column_config={
            "Total /100": st.column_config.ProgressColumn("Total /100", min_value=0, max_value=100, format="%d"),
            "M1 /50":     st.column_config.ProgressColumn("M1 /50",    min_value=0, max_value=50,  format="%d"),
            "M2 /44":     st.column_config.ProgressColumn("M2 /44",    min_value=0, max_value=44,  format="%d"),
            "Bit /6":     st.column_config.ProgressColumn("Bit /6",    min_value=0, max_value=6,   format="%d"),
        }
    )

# ══════════════════════════════════════════════════════════════════════════════
# VISTA 2 — POR ESTUDIANTE
# ══════════════════════════════════════════════════════════════════════════════
elif vista == "👤 Por estudiante":
    st.title("👤 Detalle por estudiante")

    nombres_lista = [f"{e['apellido']}, {e['nombre']}  —  {e['equipo']}" for e in estudiantes]
    seleccion     = st.selectbox("Seleccioná un estudiante", nombres_lista)
    idx           = nombres_lista.index(seleccion)
    est           = estudiantes[idx]
    nota_est      = notas.get(est["key"], {})
    m1, m2, bit, total = calcular_totales(nota_est)
    cat, color    = categoria_fadu(total)
    clases        = buscar_asistencia(est, asistencia)
    pct_asist     = (clases / TOTAL_CLASES) * 100

    st.divider()
    col_h1, col_h2 = st.columns([2, 1])
    with col_h1:
        st.markdown(f"## {est['nombre']} {est['apellido']}")
        st.caption(f"{est['equipo']}  ·  N° {est['num']}")
    with col_h2:
        st.markdown(f"### {badge(cat, color)}", unsafe_allow_html=True)
    st.divider()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total /100", f"{total:.1f}")
    c2.metric("Módulo 1 /50", f"{m1:.1f}")
    c3.metric("Módulo 2 /44", f"{m2:.1f}")
    c4.metric("Bitácoras /6", f"{bit:.1f}")
    c5.metric("Asistencia", f"{pct_asist:.0f}%", delta="OK" if pct_asist >= 85 else "En riesgo")
    st.progress(total / 100)
    st.divider()

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        st.markdown("### 🔵 Módulo 1 — /50")
        for k, cfg in MODULOS["M1"]["items"].items():
            v = nota_est.get(k, 0)
            st.markdown(f"**{cfg['label']}** `{cfg['fecha']}`")
            st.markdown(barra(v, cfg['max'], "#1a6b8a"), unsafe_allow_html=True)
            st.write("")
        st.markdown(f"**Subtotal: {m1}/50**")

    with col_b:
        st.markdown("### 🟢 Módulo 2 — /44")
        for k, cfg in MODULOS["M2"]["items"].items():
            v = nota_est.get(k, 0)
            st.markdown(f"**{cfg['label']}** `{cfg['fecha']}`")
            st.markdown(barra(v, cfg['max'], "#2e6b4f"), unsafe_allow_html=True)
            st.write("")
        st.markdown(f"**Subtotal: {m2}/44**")

    with col_c:
        st.markdown("### 🟡 Bitácoras — /6")
        for k, cfg in MODULOS["BIT"]["items"].items():
            v = nota_est.get(k, 0)
            st.markdown(f"**{cfg['label']}** `{cfg['fecha']}`")
            st.markdown(barra(v, cfg['max'], "#8b6914"), unsafe_allow_html=True)
            st.write("")
        st.markdown(f"**Subtotal: {bit}/6**")

    st.divider()
    st.markdown("### 📅 Asistencia")
    ca1, ca2, ca3 = st.columns(3)
    ca1.metric("Clases asistidas", f"{clases}/{TOTAL_CLASES}")
    ca2.metric("Porcentaje",       f"{pct_asist:.1f}%")
    ca3.metric("Estado", "✅ Cumple" if pct_asist >= 85 else "⚠️ En riesgo")

# ══════════════════════════════════════════════════════════════════════════════
# VISTA 3 — INGRESAR NOTAS
# ══════════════════════════════════════════════════════════════════════════════
elif vista == "✏️ Ingresar notas":
    st.title("✏️ Ingresar / editar notas")

    nombres_lista = [f"{e['apellido']}, {e['nombre']}  —  {e['equipo']}" for e in estudiantes]
    seleccion     = st.selectbox("Seleccioná un estudiante", nombres_lista)
    idx           = nombres_lista.index(seleccion)
    est           = estudiantes[idx]
    nota_est      = notas.get(est["key"], {}).copy()

    st.caption(f"**{est['equipo']}** · N° {est['num']}")
    st.divider()

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown("### 🔵 Módulo 1 — /50")
        for k, cfg in MODULOS["M1"]["items"].items():
            nota_est[k] = st.number_input(
                f"{cfg['label']} · {cfg['fecha']} (máx {cfg['max']})",
                min_value=0.0, max_value=float(cfg["max"]),
                value=float(nota_est.get(k, 0)),
                step=0.5, key=f"inp_m1_{k}"
            )

    with col2:
        st.markdown("### 🟢 Módulo 2 — /44")
        for k, cfg in MODULOS["M2"]["items"].items():
            nota_est[k] = st.number_input(
                f"{cfg['label']} · {cfg['fecha']} (máx {cfg['max']})",
                min_value=0.0, max_value=float(cfg["max"]),
                value=float(nota_est.get(k, 0)),
                step=0.5, key=f"inp_m2_{k}"
            )

    with col3:
        st.markdown("### 🟡 Bitácoras — /6")
        for k, cfg in MODULOS["BIT"]["items"].items():
            nota_est[k] = st.number_input(
                f"{cfg['label']} · {cfg['fecha']} (máx {cfg['max']})",
                min_value=0.0, max_value=float(cfg["max"]),
                value=float(nota_est.get(k, 0)),
                step=0.5, key=f"inp_bit_{k}"
            )

    m1, m2, bit, total = calcular_totales(nota_est)
    cat, color = categoria_fadu(total)

    st.divider()
    st.markdown("### Vista previa")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Módulo 1", f"{m1}/50")
    c2.metric("Módulo 2", f"{m2}/44")
    c3.metric("Bitácoras", f"{bit}/6")
    c4.metric("TOTAL", f"{total}/100")
    st.progress(total / 100)
    st.markdown(f"**Categoría estimada:** {badge(cat, color)}", unsafe_allow_html=True)

    st.divider()
    if st.button("💾 Guardar notas", type="primary", use_container_width=True):
        with st.spinner("Guardando en Google Sheets..."):
            save_nota_sheet(est, nota_est)
            load_notas_sheet.clear()
        st.success(f"✅ Notas de **{est['nombre']} {est['apellido']}** guardadas en Google Sheets.")

# ══════════════════════════════════════════════════════════════════════════════
# VISTA 4 — PASAR LISTA
# ══════════════════════════════════════════════════════════════════════════════
elif vista == "📋 Pasar lista":
    st.title("📋 Pasar lista")

    # Obtener código actual del servidor
    try:
        r = requests.get(f"{API_URL}/", timeout=5)
        info = r.json()
        codigo_actual = info.get("codigo_actual", "—")
    except Exception:
        codigo_actual = "Sin conexión"

    col_cod, col_btn = st.columns([3, 1])
    with col_cod:
        st.markdown(f"## Código de hoy: `{codigo_actual}`")
        st.caption("Dictalo en voz alta para que los estudiantes lo ingresen en el formulario.")
    with col_btn:
        if st.button("🔄 Nuevo código", use_container_width=True):
            try:
                requests.get(f"{API_URL}/nuevo_codigo", timeout=5)
                st.rerun()
            except Exception:
                st.error("No se pudo generar nuevo código.")

    st.divider()

    if "nombre_clase" not in st.session_state:
        st.session_state["nombre_clase"] = ""
    clase = st.text_input("Nombre de la clase (ej: clase_05)", key="nombre_clase")

    col_qr, col_url = st.columns([1, 2])
    with col_qr:
        st.markdown("**QR para proyectar:**")
        qr_url = f"{API_URL}/qr?clase={clase}"
        try:
            r_qr = requests.get(qr_url, timeout=10)
            if r_qr.status_code == 200:
                st.image(r_qr.content, width=250)
            else:
                st.error("No se pudo cargar el QR.")
        except Exception:
            st.error("Error al conectar con el servidor.")
    with col_url:
        form_url = f"{API_URL}/form?clase={clase}"
        st.markdown("**URL del formulario:**")
        st.code(form_url)
        st.caption("Los estudiantes también pueden entrar directamente a esta URL.")

    st.divider()
    st.markdown("### Asistencia registrada hoy")
    try:
        r_att = requests.get(f"{API_URL}/attendance", timeout=10)
        if r_att.status_code == 200:
            att_data = r_att.json().get("attendance", [])
            from datetime import date
            hoy = str(date.today())
            hoy_data = [a for a in att_data if a.get("fecha", "") == hoy]
            if hoy_data:
                df_hoy = pd.DataFrame(hoy_data)[["hora", "apellido", "nombre", "email", "codigo"]]
                st.dataframe(df_hoy, use_container_width=True, hide_index=True)
                st.caption(f"Total registros hoy: {len(hoy_data)}")
            else:
                st.info("Aún no hay registros para hoy.")
    except Exception:
        st.warning("No se pudo cargar la asistencia de hoy.")

# ══════════════════════════════════════════════════════════════════════════════
# VISTA 5 — BITÁCORAS
# ══════════════════════════════════════════════════════════════════════════════
elif vista == "📓 Bitácoras":
    st.title("📓 Bitácoras")

    if st.button("🔄 Actualizar", use_container_width=False):
        st.rerun()

    try:
        r = requests.get(f"{API_URL}/bitacoras", timeout=15)
        if r.status_code != 200:
            st.error(f"Error al cargar bitácoras: {r.status_code}")
            st.stop()
        bitacoras = r.json().get("bitacoras", [])
    except Exception as e:
        st.error(f"No se pudo conectar con el servidor: {e}")
        st.stop()

    if not bitacoras:
        st.info("No se encontraron bitácoras en Drive.")
        st.stop()

    col_v, col_a, col_r = st.columns(3)
    verdes    = [b for b in bitacoras if b.get("semaforo") == "🟢"]
    amarillos = [b for b in bitacoras if b.get("semaforo") == "🟡"]
    rojos     = [b for b in bitacoras if b.get("semaforo") == "🔴"]
    col_v.metric("🟢 Al día", len(verdes))
    col_a.metric("🟡 Atrasadas", len(amarillos))
    col_r.metric("🔴 Sin actualizar", len(rojos))

    st.divider()

    filtro_sem = st.selectbox("Filtrar por estado", ["Todos", "🟢 Al día", "🟡 Atrasadas", "🔴 Sin actualizar"])

    filas_bit = []
    for b in bitacoras:
        filas_bit.append({
            "Estado": b.get("semaforo", "🔴"),
            "Nombre": b.get("nombre", "—"),
            "Última modificación": b.get("ultima_modificacion", "—"),
            "Ver": b.get("link", ""),
        })

    df_bit = pd.DataFrame(filas_bit)

    if filtro_sem == "🟢 Al día":
        df_bit = df_bit[df_bit["Estado"] == "🟢"]
    elif filtro_sem == "🟡 Atrasadas":
        df_bit = df_bit[df_bit["Estado"] == "🟡"]
    elif filtro_sem == "🔴 Sin actualizar":
        df_bit = df_bit[df_bit["Estado"] == "🔴"]

    st.dataframe(
        df_bit,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Ver": st.column_config.LinkColumn("Ver en Drive", display_text="Abrir"),
        }
    )
