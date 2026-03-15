from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime
import csv
from fastapi.responses import HTMLResponse


app = FastAPI()

# Base en memoria
students = []
attendance = []

# Código oral del día/clase
CODIGO_ACTUAL = "TALLERV1403"


class Student(BaseModel):
    nombre: str
    apellido: str
    email: str


class AttendanceRequest(BaseModel):
    email: str
    codigo: str


@app.get("/")
def home():
    return {
        "mensaje": "Servidor de asistencia funcionando",
        "codigo_actual": CODIGO_ACTUAL
    }
@app.get("/form", response_class=HTMLResponse)
def formulario_asistencia():
    return """
    <html>
        <head>
            <title>Asistencia Taller V</title>
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <style>
                body {
                    font-family: Arial, sans-serif;
                    max-width: 420px;
                    margin: 40px auto;
                    padding: 20px;
                    background: #f7f7f7;
                }
                .card {
                    background: white;
                    padding: 24px;
                    border-radius: 12px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.08);
                }
                h1 {
                    font-size: 24px;
                    margin-bottom: 10px;
                }
                p {
                    color: #555;
                    margin-bottom: 20px;
                }
                input, button {
                    width: 100%;
                    padding: 12px;
                    margin-top: 10px;
                    border-radius: 8px;
                    border: 1px solid #ccc;
                    font-size: 16px;
                    box-sizing: border-box;
                }
                button {
                    background: black;
                    color: white;
                    border: none;
                    cursor: pointer;
                }
                button:hover {
                    opacity: 0.9;
                }
                #resultado {
                    margin-top: 18px;
                    font-weight: bold;
                }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>Asistencia Taller V</h1>
                <p>Ingresá tu email y el código indicado en clase.</p>

                <input type="email" id="email" placeholder="tuemail@ejemplo.com" />
                <input type="text" id="codigo" placeholder="Código de clase" />
                <button onclick="registrar()">Registrar asistencia</button>

                <div id="resultado"></div>
            </div>

            <script>
                async function registrar() {
                    const email = document.getElementById("email").value;
                    const codigo = document.getElementById("codigo").value;
                    const resultado = document.getElementById("resultado");

                    const response = await fetch("/attendance", {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json"
                        },
                        body: JSON.stringify({ email, codigo })
                    });

                    const data = await response.json();

                    if (response.ok) {
                        resultado.innerHTML = "✅ Asistencia registrada para " + data.registro.nombre + " " + data.registro.apellido;
                        resultado.style.color = "green";
                    } else {
                        resultado.innerHTML = "❌ " + data.detail;
                        resultado.style.color = "red";
                    }
                }
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
        "email": email
    }

    students.append(nuevo)

    return {
        "mensaje": "Estudiante registrado",
        "student": nuevo
    }


@app.get("/students")
def list_students():
    return {
        "total": len(students),
        "students": students
    }


@app.get("/attendance")
def ver_asistencia():
    return {
        "total": len(attendance),
        "attendance": attendance
    }


@app.post("/attendance")
def registrar_asistencia(data: AttendanceRequest):
    email = data.email.strip().lower()
    codigo = data.codigo.strip()

    if codigo != CODIGO_ACTUAL:
        raise HTTPException(status_code=400, detail="Código incorrecto")

    estudiante = next((s for s in students if s["email"] == email), None)
    if not estudiante:
        raise HTTPException(status_code=404, detail="Estudiante no encontrado")

    ya_registrado = next((a for a in attendance if a["email"] == email), None)
    if ya_registrado:
        raise HTTPException(status_code=400, detail="La asistencia ya fue registrada")

    registro = {
        "nombre": estudiante["nombre"],
        "apellido": estudiante["apellido"],
        "email": estudiante["email"],
        "codigo_ingresado": codigo,
        "fecha": datetime.now().strftime("%Y-%m-%d"),
        "hora": datetime.now().strftime("%H:%M:%S")
    }

    attendance.append(registro)

    return {
        "mensaje": "Asistencia registrada",
        "registro": registro
    }


@app.get("/load_students")
def load_students():
    cargados = 0

    with open("data/lista_prueba.csv", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        print(reader.fieldnames)


        for row in reader:
            nombre = row["nombre"].strip()
            apellido = row["apellido"].strip()
            email = row["email"].strip().lower()

            if not nombre or not apellido or not email:
                continue

            student = {
                "nombre": nombre,
                "apellido": apellido,
                "email": email
            }

            ya_existe = next((s for s in students if s["email"] == email), None)

            if not ya_existe:
                students.append(student)
                cargados += 1

    return {
        "mensaje": "Estudiantes cargados",
        "cantidad_cargados": cargados,
        "total_estudiantes": len(students)
    }


@app.post("/reset_attendance")
def reset_attendance():
    attendance.clear()
    return {"mensaje": "Asistencia reiniciada"}


@app.post("/reset_students")
def reset_students():
    students.clear()
    return {"mensaje": "Lista de estudiantes reiniciada"}
