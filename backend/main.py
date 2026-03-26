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
import qrcode
from fastapi.responses import HTMLResponse, FileResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build

TZ_UY = pytz.timezone("America/Montevideo")

app = FastAPI()

# Base en memoria
students = []
attendance = []

# Coordenadas de la facultad y radio máximo permitido
FACULTAD_LAT = -34.910281089334184
FACULTAD_LON = -56.16376847335219
RADIO_MAX_METROS = 150

BITACORAS_FOLDER_ID = "18IOsdJGepbeHJQ-9jOV7ks3Hk3t5q0wB"


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000  # radio de la Tierra en metros
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def generar_codigo():
    letras_numeros = string.ascii_uppercase + string.digits
    sufijo = "".join(random.choices(letras_numeros, k=4))
    return f"TALLER-{sufijo}"


# URL base del sistema
# En Render usa la URL pública automáticamente
# En local cae a localhost
BASE_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv(
    "BASE_URL", "http://localhost:8000"
)

# Código oral del día/clase
CODIGO_ACTUAL = generar_codigo()


@app.on_event("startup")
def startup():
    try:
        load_students()
    except Exception as e:
        print("Error cargando estudiantes:", e)
    try:
        cargar_asistencia_csv()
    except Exception as e:
        print("Error cargando asistencia:", e)


def cargar_asistencia_csv():
    archivo = "data/asistencia.csv"
    if not os.path.isfile(archivo):
        return
    with open(archivo, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = row.get("email", "").strip().lower()
            clase = row.get("clase", "").strip()
            ya_existe = next(
                (a for a in attendance if a["email"] == email and a.get("clase") == clase),
                None,
            )
            if not ya_existe:
                attendance.append({
                    "fecha": row.get("fecha", ""),
                    "clase": clase,
                    "nombre": row.get("nombre", ""),
                    "apellido": row.get("apellido", ""),
                    "email": email,
                    "codigo": row.get("codigo", ""),
                    "hora": row.get("hora", ""),
                    "ip": row.get("ip", ""),
                    "user_agent": row.get("user_agent", ""),
                    "lat": row.get("lat") or None,
                    "lon": row.get("lon") or None,
                })


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
                <input type="text" id="codigo" placeholder="Código de clase" />
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


def guardar_asistencia_csv(registro):
    os.makedirs("data", exist_ok=True)
    archivo = "data/asistencia.csv"

    existe = os.path.isfile(archivo)

    with open(archivo, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "fecha",
                "clase",
                "nombre",
                "apellido",
                "email",
                "codigo",
                "hora",
                "ip",
                "user_agent",
                "lat",
                "lon",
            ],
        )

        if not existe:
            writer.writeheader()

        writer.writerow(
            {
                "fecha": registro["fecha"],
                "clase": registro["clase"],
                "nombre": registro["nombre"],
                "apellido": registro["apellido"],
                "email": registro["email"],
                "codigo": registro["codigo"],
                "hora": registro["hora"],
                "ip": registro["ip"],
                "user_agent": registro["user_agent"],
                "lat": registro["lat"],
                "lon": registro["lon"],
            }
        )


@app.post("/attendance")
def registrar_asistencia(data: AttendanceRequest, request: Request):
    email = data.email.strip().lower()
    codigo = data.codigo.strip()

    if codigo != CODIGO_ACTUAL:
        raise HTTPException(status_code=400, detail="Código incorrecto")

    estudiante = next((s for s in students if s["email"] == email), None)
    if not estudiante:
        raise HTTPException(status_code=404, detail="Estudiante no encontrado")

    if data.lat is None or data.lon is None:
        raise HTTPException(status_code=400, detail="No se recibió la ubicación. Recargá la página y permitir acceso a la ubicación.")

    distancia = haversine(data.lat, data.lon, FACULTAD_LAT, FACULTAD_LON)
    if distancia > RADIO_MAX_METROS:
        raise HTTPException(
            status_code=400,
            detail=f"Tu ubicación está a {int(distancia)} metros de la facultad. Debés estar presente en el edificio para registrar asistencia."
        )

    clase_actual = data.clase if data.clase else "sin_clase"

    ya_registrado = next(
        (a for a in attendance if a["email"] == email and a.get("clase") == clase_actual),
        None,
    )

    if ya_registrado:
        raise HTTPException(status_code=400, detail="La asistencia ya fue registrada")

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

    attendance.append(registro)
    guardar_asistencia_csv(registro)

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
    global CODIGO_ACTUAL
    CODIGO_ACTUAL = generar_codigo()

    return {
        "mensaje": "Nuevo código generado",
        "codigo_actual": CODIGO_ACTUAL,
    }


@app.get("/qr")
def generar_qr(clase: str = "sin_clase"):
    url = f"{BASE_URL}/form?clase={clase}"

    os.makedirs("data", exist_ok=True)
    ruta = "data/qr_clase.png"

    img = qrcode.make(url)
    img.save(ruta)

    return FileResponse(ruta, media_type="image/png", filename="qr_clase.png")


@app.post("/reset_attendance")
def reset_attendance():
    attendance.clear()
    return {"mensaje": "Asistencia reiniciada"}


@app.post("/reset_students")
def reset_students():
    students.clear()
    return {"mensaje": "Lista de estudiantes reiniciada"}


@app.get("/bitacoras")
def ver_bitacoras():
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise HTTPException(status_code=500, detail="Credenciales de Google no configuradas")

    try:
        creds_dict = json.loads(creds_json)
        credentials = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        service = build("drive", "v3", credentials=credentials)

        results = service.files().list(
            q=f"'{BITACORAS_FOLDER_ID}' in parents and trashed=false",
            fields="files(id, name, modifiedTime, webViewLink)",
            pageSize=100,
            orderBy="name"
        ).execute()

        files = results.get("files", [])
        ahora = datetime.now(pytz.UTC)

        bitacoras = []
        for f in files:
            modified_str = f.get("modifiedTime", "")
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

            bitacoras.append({
                "nombre": f.get("name", ""),
                "ultima_modificacion": fecha_formateada,
                "dias_sin_actividad": dias,
                "semaforo": semaforo,
                "link": f.get("webViewLink", "")
            })

        # Ordenar: más inactivos primero
        bitacoras.sort(key=lambda x: x["dias_sin_actividad"], reverse=True)

        return {"total": len(bitacoras), "bitacoras": bitacoras}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al conectar con Google Drive: {str(e)}")


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
                            <th>Bitácora</th>
                            <th>Última modificación</th>
                            <th>Días sin actividad</th>
                            <th>Estado</th>
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
                            <tr>
                                <td>${i + 1}</td>
                                <td>${b.nombre}</td>
                                <td style="font-size:13px;">${b.ultima_modificacion}</td>
                                <td style="text-align:center;">${b.dias_sin_actividad === 999 ? '-' : b.dias_sin_actividad}</td>
                                <td style="text-align:center;font-size:18px;">${b.semaforo}</td>
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
            </script>
        </body>
    </html>
    """