from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
from datetime import datetime
from typing import Optional
import os
import csv
import random
import string
import math
import pytz
import json
import re
import qrcode
import threading
import queue
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
import io
from google.oauth2 import service_account
from googleapiclient.discovery import build

TZ_UY = pytz.timezone("America/Montevideo")

# Lock para evitar race conditions cuando 69 estudiantes registran al mismo tiempo
_attendance_lock = threading.Lock()

# Cola para escrituras a Sheets: un único hilo escritor evita el segfault por concurrencia
_sheets_queue: queue.Queue = queue.Queue()

def _sheets_worker():
    while True:
        registro = _sheets_queue.get()
        try:
            _guardar_asistencia_sheets_sync(registro)
        except Exception as e:
            print(f"Error en sheets_worker: {e}")
        finally:
            _sheets_queue.task_done()

threading.Thread(target=_sheets_worker, daemon=True).start()

app = FastAPI()

# Base en memoria
students = []
attendance = []

# Estado del registro: True = abierto, False = cerrado
registro_abierto = True

# Coordenadas de la facultad y radio máximo permitido
FACULTAD_LAT = -34.910281089334184
FACULTAD_LON = -56.16376847335219
RADIO_MAX_METROS = 150

BITACORAS_FOLDER_ID = "18IOsdJGepbeHJQ-9jOV7ks3Hk3t5q0wB"
ASISTENCIA_SHEET_ID = os.getenv("ASISTENCIA_SHEET_ID", "1Dw84-fNysTr4RhJt8VSJYTk-VX3e2GXYlLiv-fzrF80")
ASISTENCIA_SHEET_NAME = "Asistencia"


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000  # radio de la Tierra en metros
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def generar_codigo():
    letras_numeros = string.ascii_uppercase + string.digits
    return "".join(random.choices(letras_numeros, k=6))


# URL base del sistema
_railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
BASE_URL = (
    os.getenv("RENDER_EXTERNAL_URL")
    or ("https://" + _railway_domain if _railway_domain else None)
    or os.getenv("BASE_URL", "http://localhost:8000")
)

# Código oral del día/clase — persiste en archivo para sobrevivir reinicios
CODIGO_FILE = "data/codigo_actual.txt"

def _cargar_codigo():
    try:
        if os.path.exists(CODIGO_FILE):
            with open(CODIGO_FILE) as f:
                c = f.read().strip()
                if c:
                    return c
    except Exception:
        pass
    return generar_codigo()

def _guardar_codigo(codigo):
    os.makedirs("data", exist_ok=True)
    with open(CODIGO_FILE, "w") as f:
        f.write(codigo)

def _normalizar_codigo(c: str) -> str:
    """Tolera minúsculas, distintos tipos de guiones y espacios."""
    return c.strip().upper().replace("–", "-").replace("—", "-").replace(" ", "")

CODIGO_ACTUAL = _cargar_codigo()
_guardar_codigo(CODIGO_ACTUAL)


@app.on_event("startup")
def startup():
    print("BASE_URL:", BASE_URL)
    print("GCP_JSON presente:", bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("GCP_JSON")))
    print("ENV KEYS:", [k for k in os.environ.keys()])
    try:
        load_students()
    except Exception as e:
        print("Error cargando estudiantes:", e)
    try:
        cargar_asistencia_sheets()
    except Exception as e:
        print("Error cargando asistencia desde Sheets:", e)
    # CSV local como respaldo: agrega registros que puedan faltar en Sheets
    cargar_asistencia_csv()


_sheets_creds_json = None

def get_sheets_service():
    global _sheets_creds_json
    if _sheets_creds_json is None:
        _sheets_creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("GCP_JSON")
    if not _sheets_creds_json:
        raise Exception("Credenciales de Google no configuradas")
    creds_dict = json.loads(_sheets_creds_json)
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=credentials)


def cargar_asistencia_sheets():
    try:
        service = get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=ASISTENCIA_SHEET_ID,
            range=f"{ASISTENCIA_SHEET_NAME}!A:K"
        ).execute()
        rows = result.get("values", [])
        if len(rows) <= 1:
            return  # solo encabezado o vacío
        headers = rows[0]
        for row in rows[1:]:
            # completar columnas faltantes
            while len(row) < len(headers):
                row.append("")
            record = dict(zip(headers, row))
            email = record.get("email", "").strip().lower()
            clase = record.get("clase", "").strip()
            fecha = record.get("fecha", "").strip()
            ya_existe = next(
                (a for a in attendance
                 if a["email"] == email and a.get("clase") == clase and a.get("fecha") == fecha),
                None,
            )
            if not ya_existe:
                attendance.append({
                    "fecha": record.get("fecha", ""),
                    "clase": clase,
                    "nombre": record.get("nombre", ""),
                    "apellido": record.get("apellido", ""),
                    "email": email,
                    "codigo": record.get("codigo", ""),
                    "hora": record.get("hora", ""),
                    "ip": record.get("ip", ""),
                    "user_agent": record.get("user_agent", ""),
                    "lat": record.get("lat") or None,
                    "lon": record.get("lon") or None,
                })
        print(f"Asistencia cargada desde Sheets: {len(attendance)} registros")
    except Exception as e:
        print(f"Error cargando asistencia desde Sheets: {e}")


class Student(BaseModel):
    nombre: str
    apellido: str
    email: str


class AttendanceRequest(BaseModel):
    email: str
    codigo: str
    clase: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None


@app.get("/")
def home():
    return {
        "mensaje": "Servidor de asistencia funcionando",
        "codigo_actual": CODIGO_ACTUAL,
        "base_url": BASE_URL,
        "registro_abierto": registro_abierto,
    }


@app.get("/form", response_class=HTMLResponse)
def formulario_asistencia(clase: str = Query(default="sin_clase")):
    return f"""
    <html>
        <head>
            <title>Asistencia Taller V</title>
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    max-width: 420px;
                    margin: 40px auto;
                    padding: 20px;
                    background: #f7f7f7;
                }}
                .card {{
                    background: white;
                    padding: 24px;
                    border-radius: 12px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.08);
                }}
                h1 {{
                    font-size: 24px;
                    margin-bottom: 10px;
                }}
                p {{
                    color: #555;
                    margin-bottom: 20px;
                }}
                .clase {{
                    font-size: 14px;
                    color: #222;
                    background: #efefef;
                    padding: 10px;
                    border-radius: 8px;
                    margin-bottom: 16px;
                }}
                input, button {{
                    width: 100%;
                    padding: 12px;
                    margin-top: 10px;
                    border-radius: 8px;
                    border: 1px solid #ccc;
                    font-size: 16px;
                    box-sizing: border-box;
                }}
                button {{
                    background: black;
                    color: white;
                    border: none;
                    cursor: pointer;
                }}
                button:hover {{
                    opacity: 0.9;
                }}
                button:disabled {{
                    background: #999;
                    cursor: not-allowed;
                }}
                #geo-status {{
                    margin-top: 12px;
                    font-size: 14px;
                    color: #555;
                }}
                #resultado {{
                    margin-top: 18px;
                    font-weight: bold;
                }}
            </style>
        </head>
        <body>
            <div class="card">
                <h1>Asistencia Taller V</h1>
                <div class="clase">Clase: {clase}</div>
                <p>Ingresá tu email y el código indicado en clase.</p>

                <input type="email" id="email" placeholder="tuemail@ejemplo.com" />
                <input type="text" id="codigo" placeholder="Ej: A3BX7K" oninput="this.value=this.value.toUpperCase()" autocomplete="off" autocorrect="off" spellcheck="false" maxlength="6" />
                <button id="btn" onclick="registrar()" disabled>Verificando ubicación...</button>

                <div id="geo-status">📍 Obteniendo tu ubicación...</div>
                <div id="resultado"></div>
            </div>

            <script>
                const clase = "{clase}";
                let userLat = null;
                let userLon = null;

                function initGeo() {{
                    if (!navigator.geolocation) {{
                        document.getElementById("geo-status").innerHTML =
                            "❌ Tu navegador no soporta geolocalización.";
                        return;
                    }}

                    navigator.geolocation.getCurrentPosition(
                        function(pos) {{
                            userLat = pos.coords.latitude;
                            userLon = pos.coords.longitude;
                            document.getElementById("geo-status").innerHTML =
                                "✅ Ubicación obtenida";
                            document.getElementById("geo-status").style.color = "green";
                            document.getElementById("btn").disabled = false;
                            document.getElementById("btn").textContent = "Registrar asistencia";
                        }},
                        function(err) {{
                            document.getElementById("geo-status").innerHTML =
                                "❌ Debés permitir el acceso a tu ubicación para registrar asistencia.";
                            document.getElementById("geo-status").style.color = "red";
                        }},
                        {{ enableHighAccuracy: true, timeout: 15000 }}
                    );
                }}

                async function registrar() {{
                    const email = document.getElementById("email").value;
                    const codigo = document.getElementById("codigo").value;
                    const resultado = document.getElementById("resultado");

                    if (!userLat || !userLon) {{
                        resultado.innerHTML = "❌ No se pudo obtener tu ubicación. Recargá la página y permitir acceso.";
                        resultado.style.color = "red";
                        return;
                    }}

                    try {{
                        const response = await fetch("/attendance", {{
                            method: "POST",
                            headers: {{
                                "Content-Type": "application/json"
                            }},
                            body: JSON.stringify({{ email, codigo, clase, lat: userLat, lon: userLon }})
                        }});

                        const data = await response.json();

                        if (response.ok) {{
                            resultado.innerHTML =
                                "✅ Asistencia registrada para " +
                                data.registro.nombre + " " +
                                data.registro.apellido + " (" +
                                data.registro.clase + ") - " +
                                data.registro.fecha;
                            resultado.style.color = "green";
                        }} else {{
                            resultado.innerHTML = "❌ " + data.detail;
                            resultado.style.color = "red";
                        }}
                    }} catch (error) {{
                        resultado.innerHTML = "❌ Error de conexión con el servidor";
                        resultado.style.color = "red";
                    }}
                }}

                initGeo();
            </script>
        </body>
    </html>
    """


@app.post("/students")
def create_student(student: Student):
    email = student.email.strip().lower()

    ya_existe = next((s for s in students if s["email"] == email), None)
    if ya_existe:
        raise HTTPException(status_code=400, detail="El estudiante ya existe")

    nuevo = {
        "nombre": student.nombre.strip(),
        "apellido": student.apellido.strip(),
        "email": email,
    }

    students.append(nuevo)

    return {
        "mensaje": "Estudiante registrado",
        "student": nuevo,
    }


@app.get("/students")
def list_students():
    return {
        "total": len(students),
        "students": students,
    }


@app.get("/attendance")
def ver_asistencia():
    return {
        "total": len(attendance),
        "attendance": attendance,
    }


CSV_ASISTENCIA = "data/asistencia_registro.csv"
CSV_CAMPOS = ["fecha", "clase", "nombre", "apellido", "email", "codigo", "hora", "ip", "user_agent", "lat", "lon"]

def guardar_asistencia_csv(registro):
    """Guarda el registro en CSV local. Rápido y siempre funciona."""
    os.makedirs("data", exist_ok=True)
    escribir_header = not os.path.exists(CSV_ASISTENCIA)
    with open(CSV_ASISTENCIA, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_CAMPOS, extrasaction="ignore")
        if escribir_header:
            writer.writeheader()
        writer.writerow({
            **registro,
            "lat": str(registro["lat"]) if registro["lat"] is not None else "",
            "lon": str(registro["lon"]) if registro["lon"] is not None else "",
        })


def cargar_asistencia_csv():
    """Carga registros del CSV local al arrancar (complementa lo de Sheets)."""
    if not os.path.exists(CSV_ASISTENCIA):
        return
    try:
        with open(CSV_ASISTENCIA, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for record in reader:
                email = record.get("email", "").strip().lower()
                clase = record.get("clase", "").strip()
                fecha = record.get("fecha", "").strip()
                ya_existe = next(
                    (a for a in attendance
                     if a["email"] == email and a.get("clase") == clase and a.get("fecha") == fecha),
                    None,
                )
                if not ya_existe:
                    attendance.append({
                        "fecha": fecha,
                        "clase": clase,
                        "nombre": record.get("nombre", ""),
                        "apellido": record.get("apellido", ""),
                        "email": email,
                        "codigo": record.get("codigo", ""),
                        "hora": record.get("hora", ""),
                        "ip": record.get("ip", ""),
                        "user_agent": record.get("user_agent", ""),
                        "lat": record.get("lat") or None,
                        "lon": record.get("lon") or None,
                    })
        print(f"Asistencia cargada desde CSV local: {len(attendance)} registros totales")
    except Exception as e:
        print(f"Error cargando CSV local: {e}")


def _guardar_asistencia_sheets_sync(registro):
    try:
        service = get_sheets_service()
        # Verificar si existe encabezado
        result = service.spreadsheets().values().get(
            spreadsheetId=ASISTENCIA_SHEET_ID,
            range=f"{ASISTENCIA_SHEET_NAME}!A1:K1"
        ).execute()
        if not result.get("values"):
            headers = [["fecha", "clase", "nombre", "apellido", "email", "codigo", "hora", "ip", "user_agent", "lat", "lon"]]
            service.spreadsheets().values().update(
                spreadsheetId=ASISTENCIA_SHEET_ID,
                range=f"{ASISTENCIA_SHEET_NAME}!A1",
                valueInputOption="RAW",
                body={"values": headers}
            ).execute()

        fila = [[
            registro["fecha"],
            registro["clase"],
            registro["nombre"],
            registro["apellido"],
            registro["email"],
            registro["codigo"],
            registro["hora"],
            registro["ip"],
            registro["user_agent"],
            str(registro["lat"]) if registro["lat"] is not None else "",
            str(registro["lon"]) if registro["lon"] is not None else "",
        ]]
        service.spreadsheets().values().append(
            spreadsheetId=ASISTENCIA_SHEET_ID,
            range=f"{ASISTENCIA_SHEET_NAME}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": fila}
        ).execute()
    except Exception as e:
        print(f"Error guardando asistencia en Sheets: {e}")


@app.post("/attendance")
def registrar_asistencia(data: AttendanceRequest, request: Request):
    if not registro_abierto:
        raise HTTPException(status_code=403, detail="El registro de asistencia está cerrado.")

    email = data.email.strip().lower()
    codigo = data.codigo.strip()

    if _normalizar_codigo(codigo) != _normalizar_codigo(CODIGO_ACTUAL):
        raise HTTPException(status_code=400, detail="Código incorrecto")

    estudiante = next((s for s in students if s["email"] == email), None)
    if not estudiante:
        raise HTTPException(status_code=404, detail="Estudiante no encontrado")

    bypass_geo = os.getenv("BYPASS_GEO", "false").lower() == "true"

    if not bypass_geo:
        if data.lat is None or data.lon is None:
            raise HTTPException(status_code=400, detail="No se recibió la ubicación. Recargá la página y permitir acceso a la ubicación.")

        distancia = haversine(data.lat, data.lon, FACULTAD_LAT, FACULTAD_LON)
        if distancia > RADIO_MAX_METROS:
            raise HTTPException(
                status_code=400,
                detail=f"Tu ubicación está a {int(distancia)} metros de la facultad. Debés estar presente en el edificio para registrar asistencia."
            )

    clase_actual = data.clase if data.clase else "sin_clase"
    hoy = datetime.now(TZ_UY).strftime("%Y-%m-%d")

    # Detectar IP real
    forwarded_for = request.headers.get("x-forwarded-for")
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        ip_cliente = cf_ip
    elif forwarded_for:
        ip_cliente = forwarded_for.split(",")[0].strip()
    else:
        ip_cliente = request.client.host if request.client else "desconocida"

    user_agent = request.headers.get("user-agent", "desconocido")
    ahora = datetime.now(TZ_UY)

    registro = {
        "fecha": ahora.strftime("%Y-%m-%d"),
        "clase": clase_actual,
        "nombre": estudiante["nombre"],
        "apellido": estudiante["apellido"],
        "email": estudiante["email"],
        "codigo": codigo,
        "hora": ahora.strftime("%H:%M:%S"),
        "ip": ip_cliente,
        "user_agent": user_agent,
        "lat": data.lat,
        "lon": data.lon,
    }

    # Lock: evita que dos requests simultáneos del mismo email pasen el chequeo
    with _attendance_lock:
        ya_registrado = next(
            (a for a in attendance
             if a["email"] == email
             and a.get("clase") == clase_actual
             and a.get("fecha") == hoy),
            None,
        )
        if ya_registrado:
            raise HTTPException(status_code=400, detail="La asistencia ya fue registrada hoy")

        # 1. Guardar en memoria (inmediato)
        attendance.append(registro)

    # 2. Guardar en CSV local (rápido, siempre funciona, backup confiable)
    try:
        guardar_asistencia_csv(registro)
    except Exception as e:
        print(f"Error guardando CSV: {e}")

    # 3. Encolar escritura a Sheets (un único hilo escritor evita segfault por concurrencia SSL)
    _sheets_queue.put(registro)

    return {
        "mensaje": "Asistencia registrada",
        "registro": registro,
    }


@app.get("/load_students")
def load_students():
    cargados = 0
    archivo = "data/lista_prueba.csv"

    if not os.path.isfile(archivo):
        raise HTTPException(status_code=404, detail="No se encontró el archivo de estudiantes")

    with open(archivo, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        fieldnames = reader.fieldnames

        if not fieldnames or "email" not in fieldnames:
            f.seek(0)
            reader = csv.DictReader(f, delimiter=",")
            fieldnames = reader.fieldnames

        print("Columnas detectadas:", fieldnames)

        for row in reader:
            nombre = row.get("nombre", "").strip()
            apellido = row.get("apellido", "").strip()
            email = row.get("email", "").strip().lower()

            if not nombre or not apellido or not email:
                continue

            student = {
                "nombre": nombre,
                "apellido": apellido,
                "email": email,
            }

            ya_existe = next((s for s in students if s["email"] == email), None)

            if not ya_existe:
                students.append(student)
                cargados += 1

    return {
        "mensaje": "Estudiantes cargados",
        "cantidad_cargados": cargados,
        "total_estudiantes": len(students),
    }


@app.get("/nuevo_codigo")
def nuevo_codigo():
    global CODIGO_ACTUAL, registro_abierto
    CODIGO_ACTUAL = generar_codigo()
    _guardar_codigo(CODIGO_ACTUAL)
    registro_abierto = True  # nuevo código = registro abierto
    return {
        "mensaje": "Nuevo código generado",
        "codigo_actual": CODIGO_ACTUAL,
        "registro_abierto": registro_abierto,
    }


@app.post("/abrir_registro")
def abrir_registro():
    global registro_abierto
    registro_abierto = True
    return {"mensaje": "Registro abierto", "registro_abierto": True}


@app.post("/cerrar_registro")
def cerrar_registro():
    global registro_abierto
    registro_abierto = False
    return {"mensaje": "Registro cerrado", "registro_abierto": False}


@app.delete("/attendance")
def borrar_registro(email: str = Query(...), clase: str = Query(...), fecha: str = Query(...)):
    """Elimina un registro específico de la memoria y del CSV."""
    global attendance
    email = email.strip().lower()
    antes = len(attendance)
    attendance = [
        a for a in attendance
        if not (a["email"] == email and a.get("clase") == clase and a.get("fecha") == fecha)
    ]
    eliminados = antes - len(attendance)
    if eliminados == 0:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    # Reescribir CSV sin ese registro
    try:
        if os.path.exists(CSV_ASISTENCIA):
            registros_csv = []
            with open(CSV_ASISTENCIA, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if not (row.get("email","").strip().lower() == email
                            and row.get("clase","").strip() == clase
                            and row.get("fecha","").strip() == fecha):
                        registros_csv.append(row)
            with open(CSV_ASISTENCIA, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_CAMPOS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(registros_csv)
    except Exception as e:
        print(f"Error actualizando CSV al borrar: {e}")
    return {"mensaje": f"Registro eliminado", "eliminados": eliminados}


@app.get("/qr")
def generar_qr(clase: str = "sin_clase"):
    url = f"{BASE_URL}/form?clase={clase}"

    os.makedirs("data", exist_ok=True)
    ruta = "data/qr_clase.png"

    img = qrcode.make(url)
    img.save(ruta)

    return FileResponse(ruta, media_type="image/png", filename="qr_clase.png")


@app.get("/descargar_asistencia")
def descargar_asistencia(fecha: str = Query(default=None)):
    """
    Descarga la asistencia como CSV.
    Si se pasa ?fecha=2026-03-26 filtra ese día.
    Sin parámetro devuelve todo el historial.
    """
    campos = ["fecha", "clase", "nombre", "apellido", "email", "codigo", "hora", "ip", "user_agent", "lat", "lon"]

    if fecha:
        registros = [r for r in attendance if r.get("fecha") == fecha]
        nombre_archivo = f"asistencia_{fecha}.csv"
    else:
        registros = attendance
        nombre_archivo = "asistencia_completa.csv"

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=campos, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(registros)
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={nombre_archivo}"}
    )


@app.post("/sync_to_sheets")
def sync_to_sheets():
    """Sube todos los registros en memoria a Google Sheets (limpia y reescribe)."""
    if not attendance:
        return {"mensaje": "No hay registros en memoria para sincronizar", "total": 0}
    try:
        service = get_sheets_service()
        # Escribir encabezado
        headers = [["fecha", "clase", "nombre", "apellido", "email", "codigo", "hora", "ip", "user_agent", "lat", "lon"]]
        service.spreadsheets().values().update(
            spreadsheetId=ASISTENCIA_SHEET_ID,
            range=f"{ASISTENCIA_SHEET_NAME}!A1",
            valueInputOption="RAW",
            body={"values": headers}
        ).execute()
        # Escribir todos los registros
        filas = [[
            r["fecha"], r["clase"], r["nombre"], r["apellido"], r["email"],
            r["codigo"], r["hora"], r["ip"], r["user_agent"],
            str(r["lat"]) if r["lat"] is not None else "",
            str(r["lon"]) if r["lon"] is not None else "",
        ] for r in attendance]
        service.spreadsheets().values().update(
            spreadsheetId=ASISTENCIA_SHEET_ID,
            range=f"{ASISTENCIA_SHEET_NAME}!A2",
            valueInputOption="RAW",
            body={"values": filas}
        ).execute()
        return {"mensaje": f"Sincronizados {len(filas)} registros a Google Sheets", "total": len(filas)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error sincronizando: {e}")


@app.post("/reload_attendance")
def reload_attendance():
    """Recarga la asistencia desde Google Sheets sin reiniciar el servidor."""
    attendance.clear()
    try:
        cargar_asistencia_sheets()
        return {"mensaje": "Asistencia recargada desde Sheets", "total": len(attendance)}
    except Exception as e:
        return {"mensaje": f"Error recargando: {e}", "total": 0}


@app.post("/reset_attendance")
def reset_attendance():
    attendance.clear()
    return {"mensaje": "Asistencia reiniciada"}


@app.post("/migrar_csv_a_sheets")
def migrar_csv_a_sheets():
    """Migración única: sube el CSV histórico a Google Sheets."""
    archivo = "data/asistencia.csv"
    if not os.path.isfile(archivo):
        raise HTTPException(status_code=404, detail="No se encontró data/asistencia.csv")

    filas = []
    with open(archivo, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            filas.append([
                row.get("fecha", ""),
                row.get("clase", ""),
                row.get("nombre", ""),
                row.get("apellido", ""),
                row.get("email", "").strip().lower(),
                row.get("codigo", ""),
                row.get("hora", ""),
                row.get("ip", ""),
                row.get("user_agent", ""),
                row.get("lat", ""),
                row.get("lon", ""),
            ])

    if not filas:
        return {"mensaje": "CSV vacío, nada para migrar"}

    service = get_sheets_service()

    # Escribir encabezado
    headers = [["fecha", "clase", "nombre", "apellido", "email", "codigo", "hora", "ip", "user_agent", "lat", "lon"]]
    service.spreadsheets().values().update(
        spreadsheetId=ASISTENCIA_SHEET_ID,
        range=f"{ASISTENCIA_SHEET_NAME}!A1",
        valueInputOption="RAW",
        body={"values": headers}
    ).execute()

    # Escribir datos
    service.spreadsheets().values().update(
        spreadsheetId=ASISTENCIA_SHEET_ID,
        range=f"{ASISTENCIA_SHEET_NAME}!A2",
        valueInputOption="RAW",
        body={"values": filas}
    ).execute()

    return {"mensaje": f"Migración completa", "registros": len(filas)}


@app.post("/reset_students")
def reset_students():
    students.clear()
    return {"mensaje": "Lista de estudiantes reiniciada"}


@app.get("/bitacoras")
def ver_bitacoras():
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("GCP_JSON")
    if not creds_json:
        raise HTTPException(status_code=500, detail="Credenciales de Google no configuradas")

    try:
        # Cargar lista de estudiantes para hacer match por email
        estudiantes_por_email = {}
        archivo_lista = "data/lista_prueba.csv"
        if os.path.isfile(archivo_lista):
            with open(archivo_lista, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f, delimiter=";")
                for row in reader:
                    email = row.get("email", "").strip().lower()
                    nombre = row.get("nombre", "").strip().title()
                    apellido = row.get("apellido", "").strip().title()
                    if email:
                        estudiantes_por_email[email] = f"{apellido}, {nombre}"

        creds_dict = json.loads(creds_json)
        credentials = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        service = build("drive", "v3", credentials=credentials)

        results = service.files().list(
            q=f"'{BITACORAS_FOLDER_ID}' in parents and trashed=false",
            fields="files(id, name, modifiedTime, webViewLink, mimeType, shortcutDetails, owners)",
            pageSize=100,
            orderBy="name"
        ).execute()

        files = results.get("files", [])
        ahora = datetime.now(pytz.UTC)

        # Resolver archivos (una sola pasada, maneja accesos directos)
        resolved = []
        for f in files:
            item = {
                "name": f.get("name", ""),
                "file_id": f.get("id", ""),
                "modified_str": f.get("modifiedTime", ""),
                "link": f.get("webViewLink", ""),
                "owner_email": "",
            }
            if f.get("mimeType") == "application/vnd.google-apps.shortcut":
                target_id = (f.get("shortcutDetails") or {}).get("targetId")
                if target_id:
                    try:
                        real = service.files().get(
                            fileId=target_id,
                            fields="id, modifiedTime, webViewLink, owners"
                        ).execute()
                        item["file_id"] = real.get("id", target_id)
                        item["modified_str"] = real.get("modifiedTime", item["modified_str"])
                        item["link"] = real.get("webViewLink", item["link"])
                        owners = real.get("owners", [])
                        if owners:
                            item["owner_email"] = owners[0].get("emailAddress", "").lower()
                    except Exception:
                        pass
            else:
                owners = f.get("owners", [])
                if owners:
                    item["owner_email"] = owners[0].get("emailAddress", "").lower()
            resolved.append(item)

        # Detectar duplicados por email
        email_count = {}
        for item in resolved:
            e = item["owner_email"]
            if e:
                email_count[e] = email_count.get(e, 0) + 1

        # Construir lista final
        bitacoras = []
        for item in resolved:
            modified_str = item["modified_str"]
            if modified_str:
                modified = datetime.fromisoformat(modified_str.replace("Z", "+00:00"))
                dias = (ahora - modified).days
                fecha_formateada = modified.astimezone(TZ_UY).strftime("%d/%m %H:%M")
                if dias <= 7:
                    semaforo = "🟢"
                elif dias <= 14:
                    semaforo = "🟡"
                else:
                    semaforo = "🔴"
            else:
                dias = 999
                fecha_formateada = "Sin datos"
                semaforo = "🔴"

            owner_email = item["owner_email"]
            nombre_mostrar = estudiantes_por_email.get(owner_email) or item["name"]
            duplicada = email_count.get(owner_email, 0) > 1

            bitacoras.append({
                "nombre": nombre_mostrar,
                "nombre_archivo": item["name"],
                "file_id": item["file_id"],
                "ultima_modificacion": fecha_formateada,
                "dias_sin_actividad": dias,
                "semaforo": semaforo,
                "link": item["link"],
                "duplicada": duplicada,
                "email": owner_email,
            })

        bitacoras.sort(key=lambda x: x["dias_sin_actividad"], reverse=True)
        return {"total": len(bitacoras), "bitacoras": bitacoras}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al conectar con Google Drive: {str(e)}")


@app.get("/bitacora_detalle")
def bitacora_detalle(file_id: str = Query(...)):
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("GCP_JSON")
    if not creds_json:
        raise HTTPException(status_code=500, detail="Credenciales no configuradas")

    try:
        creds_dict = json.loads(creds_json)
        credentials = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=[
                "https://www.googleapis.com/auth/drive.readonly",
                "https://www.googleapis.com/auth/presentations.readonly",
            ]
        )
        slides_service = build("slides", "v1", credentials=credentials)

        presentation = slides_service.presentations().get(
            presentationId=file_id,
            fields="slides(pageElements(shape(text/textElements/textRun/content,placeholder/type),image))"
        ).execute()

        slides = presentation.get("slides", [])
        total_slides = len(slides)
        slides_con_contenido = 0
        slides_con_imagen = 0
        clases_detectadas = set()

        # Tipos de placeholder que son título/encabezado → ignorar siempre
        TITULOS = {"TITLE", "CENTERED_TITLE", "SUBTITLE"}
        # Placeholders de cuerpo (parte de la plantilla) → umbral alto
        CUERPO = {"BODY", "OBJECT", "PICTURE", "SLIDE_NUMBER", "FOOTER", "DATE_AND_TIME"}

        for slide in slides:
            tiene_contenido = False
            tiene_imagen = False
            texto_slide = ""

            for elem in slide.get("pageElements", []):
                shape = elem.get("shape", {})
                placeholder = shape.get("placeholder", {})
                placeholder_type = placeholder.get("type", "")

                es_titulo = placeholder_type in TITULOS
                es_cuerpo_plantilla = placeholder_type in CUERPO
                es_caja_libre = not placeholder  # texto box agregado manualmente

                if es_titulo:
                    continue  # ignorar siempre los títulos

                texto_elem = ""
                for te in shape.get("text", {}).get("textElements", []):
                    texto_elem += te.get("textRun", {}).get("content", "")
                texto_elem = texto_elem.strip()

                # Caja libre del estudiante: umbral bajo (> 10 chars)
                # Placeholder de cuerpo de plantilla: umbral alto (> 80 chars)
                umbral = 10 if es_caja_libre else 80

                if len(texto_elem) > umbral:
                    tiene_contenido = True
                    texto_slide += texto_elem + " "

                if "image" in elem:
                    tiene_imagen = True

            if tiene_contenido:
                slides_con_contenido += 1
            if tiene_imagen:
                slides_con_imagen += 1

            for m in re.findall(r'clase\s*\d+', texto_slide.lower()):
                clases_detectadas.add(m)

        if slides_con_contenido >= 3 or slides_con_imagen >= 3:
            semaforo = "🟢"
        elif slides_con_contenido >= 1 or slides_con_imagen >= 1:
            semaforo = "🟡"
        else:
            semaforo = "🔴"

        return {
            "total_slides": total_slides,
            "slides_con_contenido": slides_con_contenido,
            "slides_con_imagen": slides_con_imagen,
            "clases_detectadas": sorted(list(clases_detectadas)),
            "semaforo": semaforo,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al analizar: {str(e)}")


@app.get("/panel", response_class=HTMLResponse)
def panel_docente():
    return """
    <html>
        <head>
            <title>Panel Docente - Taller V</title>
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <style>
                * { box-sizing: border-box; margin: 0; padding: 0; }
                body {
                    font-family: Arial, sans-serif;
                    background: #f0f0f0;
                    padding: 24px;
                }
                h1 {
                    font-size: 22px;
                    margin-bottom: 20px;
                    color: #111;
                }
                .grid {
                    display: grid;
                    grid-template-columns: 1fr 1fr;
                    gap: 16px;
                    margin-bottom: 20px;
                }
                @media (max-width: 700px) {
                    .grid { grid-template-columns: 1fr; }
                }
                .card {
                    background: white;
                    border-radius: 12px;
                    padding: 20px;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.08);
                }
                .card h2 {
                    font-size: 14px;
                    color: #888;
                    text-transform: uppercase;
                    letter-spacing: 1px;
                    margin-bottom: 12px;
                }
                .codigo {
                    font-size: 36px;
                    font-weight: bold;
                    letter-spacing: 4px;
                    color: #111;
                    margin-bottom: 14px;
                }
                input[type=text] {
                    width: 100%;
                    padding: 10px 12px;
                    border: 1px solid #ddd;
                    border-radius: 8px;
                    font-size: 15px;
                    margin-bottom: 10px;
                }
                button {
                    padding: 10px 18px;
                    border: none;
                    border-radius: 8px;
                    background: black;
                    color: white;
                    font-size: 14px;
                    cursor: pointer;
                }
                button:hover { opacity: 0.85; }
                button.secondary {
                    background: #eee;
                    color: #333;
                }
                #qr-img {
                    margin-top: 14px;
                    max-width: 220px;
                    display: none;
                    border-radius: 8px;
                }
                .full-card {
                    background: white;
                    border-radius: 12px;
                    padding: 20px;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.08);
                }
                .full-card h2 {
                    font-size: 14px;
                    color: #888;
                    text-transform: uppercase;
                    letter-spacing: 1px;
                    margin-bottom: 14px;
                }
                .contador {
                    font-size: 28px;
                    font-weight: bold;
                    color: #111;
                    margin-bottom: 14px;
                }
                table {
                    width: 100%;
                    border-collapse: collapse;
                    font-size: 14px;
                }
                th {
                    text-align: left;
                    padding: 8px 10px;
                    background: #f5f5f5;
                    color: #555;
                    font-weight: 600;
                }
                td {
                    padding: 8px 10px;
                    border-bottom: 1px solid #f0f0f0;
                }
                tr:last-child td { border-bottom: none; }
                .badge {
                    display: inline-block;
                    background: #e8f5e9;
                    color: #2e7d32;
                    padding: 2px 8px;
                    border-radius: 12px;
                    font-size: 12px;
                }
                .refresh-note {
                    font-size: 12px;
                    color: #aaa;
                    margin-top: 10px;
                }
                #status-msg {
                    margin-top: 8px;
                    font-size: 13px;
                    color: green;
                    min-height: 18px;
                }
            </style>
        </head>
        <body>
            <h1>Panel Docente — Taller V</h1>

            <div class="grid">
                <!-- Código actual -->
                <div class="card">
                    <h2>Código de clase</h2>
                    <div class="codigo" id="codigo">...</div>
                    <button onclick="nuevoCodigo()">Generar nuevo código</button>
                    <div id="status-msg"></div>
                </div>

                <!-- QR -->
                <div class="card">
                    <h2>Código QR</h2>
                    <input type="text" id="nombre-clase" placeholder="Nombre de la clase (ej: Clase 7 - 26 marzo)" />
                    <button onclick="generarQR()">Generar QR</button>
                    <br/>
                    <img id="qr-img" src="" alt="QR" />
                </div>
            </div>

            <!-- Asistencia -->
            <div class="full-card">
                <h2>Asistencia</h2>
                <div style="display:flex; align-items:center; gap:12px; margin-bottom:14px; flex-wrap:wrap;">
                    <input type="date" id="filtro-fecha" style="padding:8px 10px; border:1px solid #ddd; border-radius:8px; font-size:14px;" />
                    <button class="secondary" onclick="cargarAsistencia()">Ver</button>
                    <span style="font-size:13px; color:#888;">Hoy: <a href="#" onclick="irAHoy()" style="color:#333;">volver a hoy</a></span>
                    <button class="secondary" onclick="descargarDia()" title="Descargar CSV del día seleccionado">⬇️ Día</button>
                    <button class="secondary" onclick="descargarTodo()" title="Descargar historial completo">⬇️ Todo</button>
                </div>
                <div class="contador"><span id="total">0</span> presentes</div>
                <table>
                    <thead>
                        <tr>
                            <th>#</th>
                            <th>Nombre</th>
                            <th>Apellido</th>
                            <th>Hora</th>
                            <th>Clase</th>
                            <th>IP</th>
                            <th>Dispositivo</th>
                        </tr>
                    </thead>
                    <tbody id="tabla-asistencia">
                        <tr><td colspan="7" style="color:#aaa">Cargando...</td></tr>
                    </tbody>
                </table>
                <div class="refresh-note">Se actualiza automáticamente cada 30 segundos (solo para la fecha seleccionada).</div>
            </div>

            <!-- Bitácoras -->
            <div class="full-card" style="margin-top:20px;">
                <h2>Bitácoras</h2>
                <div style="display:flex; align-items:center; gap:12px; margin-bottom:14px; flex-wrap:wrap;">
                    <button onclick="cargarBitacoras()" id="btn-bitacoras">Ver bitácoras</button>
                    <span style="font-size:12px;color:#aaa;">🟢 última semana &nbsp; 🟡 última quincena &nbsp; 🔴 más de 14 días</span>
                </div>
                <div id="bitacoras-estado" style="font-size:13px;color:#888;min-height:20px;"></div>
                <table id="tabla-bitacoras" style="display:none;">
                    <thead>
                        <tr>
                            <th>#</th>
                            <th>Estudiante</th>
                            <th>Últ. modificación</th>
                            <th>Días</th>
                            <th>Actividad</th>
                            <th>Contenido</th>
                            <th>Abrir</th>
                        </tr>
                    </thead>
                    <tbody id="tbody-bitacoras"></tbody>
                </table>
            </div>

            <script>
                const hoy = new Date().toISOString().split("T")[0];
                document.getElementById("filtro-fecha").value = hoy;

                async function cargarCodigo() {
                    const res = await fetch("/");
                    const data = await res.json();
                    document.getElementById("codigo").textContent = data.codigo_actual;
                }

                async function nuevoCodigo() {
                    const res = await fetch("/nuevo_codigo");
                    const data = await res.json();
                    document.getElementById("codigo").textContent = data.codigo_actual;
                    const msg = document.getElementById("status-msg");
                    msg.textContent = "✅ Nuevo código generado";
                    setTimeout(() => msg.textContent = "", 3000);
                }

                function generarQR() {
                    const clase = document.getElementById("nombre-clase").value.trim() || "sin_clase";
                    const img = document.getElementById("qr-img");
                    img.src = "/qr?clase=" + encodeURIComponent(clase) + "&t=" + Date.now();
                    img.style.display = "block";
                }

                function irAHoy() {
                    document.getElementById("filtro-fecha").value = hoy;
                    cargarAsistencia();
                }

                async function cargarAsistencia() {
                    const fecha = document.getElementById("filtro-fecha").value || hoy;
                    const res = await fetch("/attendance");
                    const data = await res.json();
                    const registros = data.attendance.filter(r => r.fecha === fecha);
                    registros.sort((a, b) => a.hora.localeCompare(b.hora));

                    document.getElementById("total").textContent = registros.length;

                    const tbody = document.getElementById("tabla-asistencia");
                    if (registros.length === 0) {
                        tbody.innerHTML = "<tr><td colspan='7' style='color:#aaa'>Sin registros para esta fecha.</td></tr>";
                        return;
                    }
                    tbody.innerHTML = registros.map((r, i) => `
                        <tr>
                            <td>${i + 1}</td>
                            <td>${r.nombre}</td>
                            <td>${r.apellido}</td>
                            <td><span class="badge">${r.hora}</span></td>
                            <td>${r.clase}</td>
                            <td style="font-size:12px;color:#666;">${r.ip || '-'}</td>
                            <td style="font-size:11px;color:#888;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${r.user_agent || ''}">${parsearDispositivo(r.user_agent)}</td>
                        </tr>
                    `).join("");
                }

                function parsearDispositivo(ua) {
                    if (!ua) return '-';
                    if (ua.includes('iPhone')) return '📱 iPhone';
                    if (ua.includes('Android')) {
                        if (ua.includes('SamsungBrowser')) return '📱 Samsung';
                        return '📱 Android';
                    }
                    if (ua.includes('iPad')) return '📱 iPad';
                    if (ua.includes('Macintosh') || ua.includes('Mac OS X') && !ua.includes('iPhone') && !ua.includes('iPad')) return '💻 Mac';
                    if (ua.includes('Windows')) return '💻 Windows';
                    return '🌐 Otro';
                }

                async function analizarContenido(fileId, rowIdx) {
                    const celda = document.getElementById("contenido-" + rowIdx);
                    celda.innerHTML = '<span style="font-size:12px;color:#aaa;">⏳</span>';
                    try {
                        const res = await fetch("/bitacora_detalle?file_id=" + fileId);
                        const d = await res.json();
                        if (!res.ok) {
                            celda.innerHTML = '<span style="font-size:11px;color:red;">Error</span>';
                            return;
                        }
                        const clases = d.clases_detectadas.length > 0
                            ? d.clases_detectadas.join(", ")
                            : "—";
                        celda.innerHTML = `
                            <span style="font-size:18px;">${d.semaforo}</span>
                            <span style="font-size:11px;color:#555;margin-left:4px;">
                                ${d.total_slides} slides &nbsp;
                                📝${d.slides_con_contenido} &nbsp;
                                🖼️${d.slides_con_imagen}
                            </span>
                            <br><span style="font-size:10px;color:#888;">${clases}</span>
                        `;
                    } catch(e) {
                        celda.innerHTML = '<span style="font-size:11px;color:red;">Error</span>';
                    }
                }

                async function cargarBitacoras() {
                    const btn = document.getElementById("btn-bitacoras");
                    const estado = document.getElementById("bitacoras-estado");
                    const tabla = document.getElementById("tabla-bitacoras");
                    const tbody = document.getElementById("tbody-bitacoras");

                    btn.disabled = true;
                    btn.textContent = "Cargando...";
                    estado.textContent = "⏳ Consultando Google Drive...";
                    tabla.style.display = "none";

                    try {
                        const res = await fetch("/bitacoras");
                        const data = await res.json();

                        if (!res.ok) {
                            estado.textContent = "❌ " + data.detail;
                            btn.disabled = false;
                            btn.textContent = "Ver bitácoras";
                            return;
                        }

                        estado.textContent = `${data.total} bitácoras encontradas`;
                        tabla.style.display = "table";

                        tbody.innerHTML = data.bitacoras.map((b, i) => `
                            <tr style="${b.duplicada ? 'background:#fff8e1;' : ''}" id="row-${i}">
                                <td>${i + 1}</td>
                                <td>
                                    ${b.nombre}
                                    ${b.duplicada ? '<span style="background:#ff9800;color:white;font-size:10px;padding:2px 6px;border-radius:8px;margin-left:6px;">⚠️ DUPLICADA</span>' : ''}
                                </td>
                                <td style="font-size:13px;">${b.ultima_modificacion}</td>
                                <td style="text-align:center;">${b.dias_sin_actividad === 999 ? '-' : b.dias_sin_actividad}</td>
                                <td style="text-align:center;font-size:18px;">${b.semaforo}</td>
                                <td id="contenido-${i}">
                                    <button class="secondary" style="padding:4px 10px;font-size:12px;" onclick="analizarContenido('${b.file_id}', ${i})">🔍</button>
                                </td>
                                <td><a href="${b.link}" target="_blank" style="font-size:12px;color:#333;">Abrir →</a></td>
                            </tr>
                        `).join("");

                    } catch (e) {
                        estado.textContent = "❌ Error de conexión";
                    }

                    btn.disabled = false;
                    btn.textContent = "Actualizar";
                }

                cargarCodigo();
                cargarAsistencia();
                setInterval(cargarAsistencia, 30000);

                function descargarDia() {
                    const fecha = document.getElementById("filtro-fecha").value || hoy;
                    window.location.href = "/descargar_asistencia?fecha=" + fecha;
                }

                function descargarTodo() {
                    window.location.href = "/descargar_asistencia";
                }
            </script>
        </body>
    </html>
    """