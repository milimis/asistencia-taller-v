from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
from datetime import datetime
from typing import Optional
import os
import csv
import random
import string
import math
import qrcode
from fastapi.responses import HTMLResponse, FileResponse

app = FastAPI()

# Base en memoria
students = []
attendance = []

# Coordenadas de la facultad y radio máximo permitido
FACULTAD_LAT = -34.910281089334184
FACULTAD_LON = -56.16376847335219
RADIO_MAX_METROS = 150


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
def cargar_estudiantes():
    try:
        load_students()
    except Exception as e:
        print("Error cargando estudiantes:", e)


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
    ahora = datetime.now()

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