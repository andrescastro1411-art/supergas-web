import os
import re
import secrets
import sqlite3
import string
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Form, UploadFile, File, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, Field, validator
from starlette.middleware.sessions import SessionMiddleware

# ============================================
# 1. CONFIGURACIÓN INICIAL
# ============================================
load_dotenv()

ADMIN_USER = os.getenv("ADMIN_USER")
ADMIN_PASS = os.getenv("ADMIN_PASS")
SESSION_SECRET = os.getenv("SESSION_SECRET")

if not ADMIN_USER:
    raise RuntimeError("Falta configurar ADMIN_USER en el archivo .env")

if not ADMIN_PASS:
    raise RuntimeError("Falta configurar ADMIN_PASS en el archivo .env")

if not SESSION_SECRET:
    raise RuntimeError("Falta configurar SESSION_SECRET en el archivo .env")

if len(SESSION_SECRET) < 32:
    raise RuntimeError("SESSION_SECRET debe tener al menos 32 caracteres")

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"
TEMPLATES_DIR = BASE_DIR / "templates"
DB_PATH = BASE_DIR / "app.db"
DOCUMENTS_DIR = FRONTEND_DIR / "assets" / "documentos"
CV_UPLOADS_DIR = BASE_DIR / "uploads" / "cv"

TIPOS_PQRS_VALIDOS = ["Petición", "Queja", "Reclamo", "Sugerencia", "Denuncia"]
ESTADOS_PQRS_VALIDOS = ["Pendiente", "Resuelto", "En Proceso", "Rechazado"]


# ============================================
# 2. INSTANCIA DE LA APP
# ============================================
app = FastAPI(title="Supergas de Nariño API", version="2.0.0")

# Montar archivos estáticos principales
app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")


# Sección 2.1. Archivo robots.txt
@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    robots_path = FRONTEND_DIR / "robots.txt"

    if not robots_path.exists():
        raise HTTPException(status_code=404, detail="robots.txt no encontrado")

    return FileResponse(
        robots_path,
        media_type="text/plain"
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "null",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    max_age=3600,
    same_site="lax",
    https_only=False
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# Sección 2.2. Ruta sitemap.xml
@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap():
    sitemap_path = FRONTEND_DIR / "sitemap.xml"

    if not sitemap_path.exists():
        raise HTTPException(
            status_code=404,
            detail="sitemap.xml no encontrado"
        )

    return FileResponse(
        sitemap_path,
        media_type="application/xml"
    )

# ============================================
# 3. HELPERS GENERALES
# ============================================
def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ok(data=None, message: Optional[str] = None, status_code: int = 200) -> JSONResponse:
    payload = {"success": True}
    if data is not None:
        payload["data"] = data
    if message:
        payload["message"] = message
    return JSONResponse(payload, status_code=status_code)


def fail(
    error: str,
    status_code: int = 400,
    details: Optional[dict] = None
) -> JSONResponse:
    payload = {
        "success": False,
        "error": error
    }

    if details:
        payload["details"] = details

    return JSONResponse(payload, status_code=status_code)

# ============================================
# 3.1 MANEJO GLOBAL DE ERRORES
# ============================================
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": exc.detail,
            "path": str(request.url.path)
        }
    )


@app.exception_handler(sqlite3.Error)
async def sqlite_exception_handler(request: Request, exc: sqlite3.Error):
    print(f"❌ Error SQLite en {request.url.path}: {exc}")

    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Error interno de base de datos",
            "path": str(request.url.path)
        }
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    print(f"❌ Error inesperado en {request.url.path}: {exc}")

    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Error interno del servidor",
            "path": str(request.url.path)
        }
    )



def sanitize_filename(filename: str) -> str:
    clean_name = Path(filename).name
    clean_name = re.sub(r"[^a-zA-Z0-9._-]", "_", clean_name)
    return clean_name


def is_pdf_upload(upload: UploadFile) -> bool:
    if not upload or not upload.filename:
        return False
    return upload.filename.lower().endswith(".pdf")


# ============================================
# 4. BASE DE DATOS
# ============================================
def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def ensure_column(con: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    cursor = con.execute(f"PRAGMA table_info({table_name})")
    columns = [col[1] for col in cursor.fetchall()]
    if column_name not in columns:
        con.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def init_db() -> None:
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    CV_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    with get_db() as con:
        # ============================================
        # 4.1 TABLA PQRS
        # ============================================
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS pqrs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                radicado TEXT NOT NULL UNIQUE,
                tipo TEXT NOT NULL,
                nombre TEXT NOT NULL,
                documento TEXT NOT NULL,
                email TEXT NOT NULL,
                telefono TEXT NOT NULL,
                asunto TEXT NOT NULL,
                mensaje TEXT NOT NULL,
                estado TEXT DEFAULT 'Pendiente',
                respuesta TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
            """
        )

        ensure_column(con, "pqrs", "estado", "TEXT DEFAULT 'Pendiente'")
        ensure_column(con, "pqrs", "respuesta", "TEXT")
        ensure_column(con, "pqrs", "updated_at", "TEXT")

        # ============================================
        # 4.2 TABLA NOTIFICACIONES
        # ============================================
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS notificaciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo TEXT NOT NULL,
                mensaje TEXT NOT NULL,
                leida INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )

        # ============================================
        # 4.3 TABLA ESTADÍSTICAS
        # ============================================
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS estadisticas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha TEXT UNIQUE NOT NULL,
                visitas INTEGER DEFAULT 0,
                pqrs_recibidas INTEGER DEFAULT 0,
                pqrs_resueltas INTEGER DEFAULT 0
            )
            """
        )

        # ============================================
        # 4.4 TABLA DOCUMENTOS LEGALES
        # ============================================
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS documentos_legales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo TEXT DEFAULT 'aspectos',
                categoria TEXT NOT NULL,
                nombre TEXT NOT NULL,
                descripcion TEXT,
                archivo TEXT NOT NULL,
                orden INTEGER DEFAULT 0,
                activo INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
            """
        )

        ensure_column(con, "documentos_legales", "tipo", "TEXT DEFAULT 'aspectos'")

        # ============================================
        # 4.5 TABLA SECCIONES DE TRANSPARENCIA
        # ============================================
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS secciones_transparencia (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                numero INTEGER NOT NULL,
                titulo TEXT NOT NULL,
                orden INTEGER DEFAULT 0,
                activo INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
            """
        )

        # ============================================
        # 4.6 TABLA SUBSECCIONES DE TRANSPARENCIA
        # ============================================
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS subsecciones_transparencia (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seccion_id INTEGER NOT NULL,
                nombre TEXT NOT NULL,
                link TEXT,
                orden INTEGER DEFAULT 0,
                activo INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                FOREIGN KEY (seccion_id) REFERENCES secciones_transparencia(id) ON DELETE CASCADE
            )
            """
        )

                # ============================================
        # 4.8 TABLA NOTICIAS
        # ============================================
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS noticias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                titulo TEXT NOT NULL,
                descripcion TEXT,
                tipo TEXT DEFAULT 'instagram',
                enlace TEXT,
                imagen TEXT,
                activo INTEGER DEFAULT 1,
                orden INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
            """
        )

        con.execute("CREATE INDEX IF NOT EXISTS idx_noticias_activo ON noticias(activo)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_noticias_orden ON noticias(orden)")

                # ============================================
        # 4.9 TABLA TARIFAS
        # ============================================
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS tarifas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                anio INTEGER NOT NULL,
                mes INTEGER NOT NULL,
                nombre TEXT NOT NULL,
                descripcion TEXT,
                archivo TEXT NOT NULL,
                activo INTEGER DEFAULT 1,
                orden INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
            """
        )

        con.execute("CREATE INDEX IF NOT EXISTS idx_tarifas_anio ON tarifas(anio)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_tarifas_mes ON tarifas(mes)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_tarifas_activo ON tarifas(activo)")

        # ============================================
        # 4.7 ÍNDICES
        # ============================================
        con.execute("CREATE INDEX IF NOT EXISTS idx_pqrs_radicado ON pqrs(radicado)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_pqrs_estado ON pqrs(estado)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_pqrs_email ON pqrs(email)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_pqrs_created_at ON pqrs(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_notificaciones_leida ON notificaciones(leida)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_documentos_categoria ON documentos_legales(categoria)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_documentos_activo ON documentos_legales(activo)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_documentos_tipo ON documentos_legales(tipo)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_secciones_orden ON secciones_transparencia(orden)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_subsecciones_seccion ON subsecciones_transparencia(seccion_id)")

        con.commit()

# Ejecutar una sola vez para actualizar la tabla subsecciones_transparencia
def actualizar_tabla_subsecciones():
    with get_db() as con:
        try:
            con.execute("ALTER TABLE subsecciones_transparencia ADD COLUMN tipo_contenido TEXT DEFAULT 'enlace'")
            print("✅ Columna tipo_contenido agregada")
        except sqlite3.OperationalError:
            print("ℹ️ Columna tipo_contenido ya existe")
        
        try:
            con.execute("ALTER TABLE subsecciones_transparencia ADD COLUMN archivo_pdf TEXT")
            print("✅ Columna archivo_pdf agregada")
        except sqlite3.OperationalError:
            print("ℹ️ Columna archivo_pdf ya existe")
        
        con.commit()


@app.on_event("startup")
def startup() -> None:
    init_db()
    actualizar_tabla_subsecciones()
    print("\n" + "=" * 60)
    print("🚀 SUPERGAS DE NARIÑO - BACKEND")
    print("=" * 60)
    print(f"📁 Backend: {BASE_DIR}")
    print(f"📁 Frontend: {FRONTEND_DIR}")
    print(f"📁 Base de datos: {DB_PATH}")
    print("🌐 Servidor: http://localhost:8000")
    print("🔑 Admin: http://localhost:8000/admin/login")
    print("=" * 60 + "\n")


# ============================================
# 5. AUTENTICACIÓN Y SESIÓN
# ============================================
def is_logged(request: Request) -> bool:
    return request.session.get("logged_in") is True


def require_login(request: Request) -> None:
    if not is_logged(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesión expirada o no autorizada"
        )


def verify_admin(user: str, password: str) -> bool:
    return user == ADMIN_USER and password == ADMIN_PASS


def generar_radicado() -> str:
    today = datetime.now().strftime("%Y%m%d")
    alphabet = string.ascii_uppercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"PQRS-{today}-{suffix}"


# ============================================
# 6. MODELOS PYDANTIC
# ============================================
class PQRSIn(BaseModel):
    tipo: str
    nombre: str
    documento: str
    email: EmailStr
    telefono: str
    asunto: str
    mensaje: str

    @validator("tipo")
    def validar_tipo(cls, v):
        v = v.strip()

        if not v:
            raise ValueError("El tipo es obligatorio")

        if v not in TIPOS_PQRS_VALIDOS:
            raise ValueError(
                f"Tipo debe ser uno de: {', '.join(TIPOS_PQRS_VALIDOS)}"
            )

        return v

    @validator("nombre")
    def validar_nombre(cls, v):
        v = v.strip()

        if len(v) < 3:
            raise ValueError("El nombre debe tener al menos 3 caracteres")

        if len(v) > 100:
            raise ValueError("El nombre no puede superar 100 caracteres")

        return v

    @validator("documento")
    def validar_documento(cls, v):
        v = v.strip()

        if len(v) < 5:
            raise ValueError("Documento inválido")

        if len(v) > 20:
            raise ValueError("Documento demasiado largo")

        return v

    @validator("telefono")
    def validar_telefono(cls, v):
        v = v.strip()

        if len(v) < 7:
            raise ValueError("Teléfono inválido")

        if len(v) > 20:
            raise ValueError("Teléfono demasiado largo")

        return v

    @validator("asunto")
    def validar_asunto(cls, v):
        v = v.strip()

        if len(v) < 5:
            raise ValueError("El asunto debe tener al menos 5 caracteres")

        if len(v) > 200:
            raise ValueError("El asunto es demasiado largo")

        return v

    @validator("mensaje")
    def validar_mensaje(cls, v):
        v = v.strip()

        if len(v) < 10:
            raise ValueError("El mensaje debe tener al menos 10 caracteres")

        if len(v) > 2000:
            raise ValueError("El mensaje es demasiado largo")

        return v


class PQRSRespuesta(BaseModel):
    respuesta: str = Field(..., min_length=5, max_length=2000)
    estado: str = Field(..., pattern="^(Resuelto|En Proceso|Rechazado)$")

    @validator("estado")
    def validar_estado(cls, v):
        estados_validos = ["Resuelto", "En Proceso", "Rechazado"]
        if v not in estados_validos:
            raise ValueError(f"Estado debe ser uno de: {', '.join(estados_validos)}")
        return v


class DocumentoLegalIn(BaseModel):
    categoria: str = Field(..., min_length=3, max_length=50)
    nombre: str = Field(..., min_length=3, max_length=200)
    descripcion: Optional[str] = Field(None, max_length=500)
    orden: Optional[int] = 0
    activo: Optional[int] = 1


# ============================================
# 7. RUTAS PRINCIPALES
# ============================================
@app.get("/", response_class=HTMLResponse)
def home():
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>Error: index.html no encontrado</h1>", status_code=404)
    return FileResponse(index_path)


@app.get("/index.html")
def redirect_index():
    return RedirectResponse(url="/", status_code=301)


@app.get("/pages/{page_name}")
def serve_page(page_name: str):
    if not page_name.endswith(".html"):
        page_name += ".html"
    page_path = FRONTEND_DIR / "pages" / page_name
    if not page_path.exists():
        return HTMLResponse("<h1>Página no encontrada</h1>", status_code=404)
    return FileResponse(page_path)


# ===== RUTA PARA SERVIR DOCUMENTOS PDF =====
@app.get("/assets/documentos/{filename}")
async def serve_documento(filename: str):
    if not filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Solo se permiten archivos PDF")
    
    safe_filename = Path(filename).name
    file_path = DOCUMENTS_DIR / safe_filename
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    
    return FileResponse(file_path, media_type="application/pdf", filename=filename)


@app.get("/ping")
def ping():
    return {"ping": "pong", "status": "ok", "timestamp": datetime.now().isoformat()}


# ============================================
# 8. RUTAS DE ADMINISTRACIÓN
# ============================================
@app.get("/admin/login", response_class=HTMLResponse)
def admin_login(request: Request):
    if is_logged(request):
        return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/admin/login")
async def admin_login_post(request: Request):
    form = await request.form()
    user = (form.get("user") or "").strip()
    password = (form.get("password") or "").strip()

    if not verify_admin(user, password):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Credenciales inválidas"},
            status_code=401,
        )

    request.session["logged_in"] = True
    request.session["admin_user"] = user
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_panel(request: Request):
    require_login(request)
    return FileResponse(TEMPLATES_DIR / "admin.html")


# ============================================
# 9. API PQRS
# ============================================
@app.post("/api/pqrs")
def create_pqrs(data: PQRSIn):
    radicado = generar_radicado()
    created_at = now_iso()

    with get_db() as con:
        for intento in range(3):
            try:
                con.execute(
                    """
                    INSERT INTO pqrs
                    (radicado, tipo, nombre, documento, email, telefono, asunto, mensaje, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (radicado, data.tipo, data.nombre, data.documento, str(data.email), data.telefono, data.asunto, data.mensaje, created_at),
                )
                con.execute("INSERT INTO notificaciones (tipo, mensaje, created_at) VALUES (?, ?, ?)",
                           ("Nueva PQRS", f"Se recibió PQRS {radicado}", created_at))
                con.commit()
                break
            except sqlite3.IntegrityError:
                if intento == 2:
                    raise HTTPException(status_code=500, detail="No se pudo generar radicado único")
                radicado = generar_radicado()

    return ok({"radicado": radicado}, message="PQRS creada exitosamente")


@app.get("/api/pqrs/seguimiento/{radicado}")
def seguimiento_pqrs(radicado: str):
    with get_db() as con:
        pqrs = con.execute(
            "SELECT radicado, tipo, estado, created_at, respuesta, updated_at FROM pqrs WHERE radicado = ?",
            (radicado,)
        ).fetchone()
        if not pqrs:
            return fail("Radicado no encontrado", status_code=404)
    return ok(dict(pqrs))


@app.get("/api/pqrs/listar")
def listar_pqrs(request: Request):
    require_login(request)
    with get_db() as con:
        pqrs = con.execute("SELECT * FROM pqrs ORDER BY created_at DESC").fetchall()
    return ok([dict(p) for p in pqrs])


@app.get("/api/pqrs/buscar")
def buscar_pqrs(request: Request, q: str = ""):
    require_login(request)
    query_text = q.strip()
    with get_db() as con:
        pqrs = con.execute(
            "SELECT * FROM pqrs WHERE radicado LIKE ? OR nombre LIKE ? OR email LIKE ? OR documento LIKE ? ORDER BY created_at DESC",
            (f"%{query_text}%", f"%{query_text}%", f"%{query_text}%", f"%{query_text}%")
        ).fetchall()
    return ok([dict(p) for p in pqrs])


@app.get("/api/pqrs/{pqrs_id}")
def obtener_pqrs(pqrs_id: int, request: Request):
    require_login(request)
    with get_db() as con:
        pqrs = con.execute("SELECT * FROM pqrs WHERE id = ?", (pqrs_id,)).fetchone()
        if not pqrs:
            return fail("PQRS no encontrada", status_code=404)
    return ok(dict(pqrs))


@app.post("/api/pqrs/{pqrs_id}/responder")
def responder_pqrs(pqrs_id: int, data: PQRSRespuesta, request: Request):
    require_login(request)
    updated_at = now_iso()
    with get_db() as con:
        pqrs = con.execute("SELECT * FROM pqrs WHERE id = ?", (pqrs_id,)).fetchone()
        if not pqrs:
            return fail("PQRS no encontrada", status_code=404)
        con.execute("UPDATE pqrs SET respuesta = ?, estado = ?, updated_at = ? WHERE id = ?",
                   (data.respuesta, data.estado, updated_at, pqrs_id))
        con.execute("INSERT INTO notificaciones (tipo, mensaje, created_at) VALUES (?, ?, ?)",
                   ("PQRS Respondida", f"Se respondió PQRS {pqrs['radicado']}", updated_at))
        con.commit()
    return ok(message="PQRS actualizada correctamente")


@app.delete("/api/pqrs/{pqrs_id}")
def eliminar_pqrs(pqrs_id: int, request: Request):
    require_login(request)
    with get_db() as con:
        cursor = con.execute("DELETE FROM pqrs WHERE id = ?", (pqrs_id,))
        con.commit()
    if cursor.rowcount == 0:
        return fail("PQRS no encontrada", status_code=404)
    return ok(message="PQRS eliminada correctamente")

# ============================================
# 9.1 API ESTADÍSTICAS DEL DASHBOARD ADMIN
# ============================================
@app.get("/api/stats")
def obtener_estadisticas_dashboard(request: Request):
    require_login(request)

    with get_db() as con:
        total = con.execute(
            "SELECT COUNT(*) AS total FROM pqrs"
        ).fetchone()["total"]

        por_estado_raw = con.execute(
            """
            SELECT estado, COUNT(*) AS count
            FROM pqrs
            GROUP BY estado
            """
        ).fetchall()

        por_tipo_raw = con.execute(
            """
            SELECT tipo, COUNT(*) AS count
            FROM pqrs
            GROUP BY tipo
            """
        ).fetchall()

    estados_dict = {row["estado"]: row["count"] for row in por_estado_raw}
    tipos_dict = {row["tipo"]: row["count"] for row in por_tipo_raw}

    por_estado = [
        {"estado": "Pendiente", "count": estados_dict.get("Pendiente", 0)},
        {"estado": "En Proceso", "count": estados_dict.get("En Proceso", 0)},
        {"estado": "Resuelto", "count": estados_dict.get("Resuelto", 0)},
        {"estado": "Rechazado", "count": estados_dict.get("Rechazado", 0)},
    ]

    por_tipo = [
        {"tipo": "Petición", "count": tipos_dict.get("Petición", 0)},
        {"tipo": "Queja", "count": tipos_dict.get("Queja", 0)},
        {"tipo": "Reclamo", "count": tipos_dict.get("Reclamo", 0)},
        {"tipo": "Sugerencia", "count": tipos_dict.get("Sugerencia", 0)},
        {"tipo": "Denuncia", "count": tipos_dict.get("Denuncia", 0)},
    ]

    return ok({
        "total": total,
        "por_estado": por_estado,
        "por_tipo": por_tipo
    })


# ============================================
# 10. API DOCUMENTOS LEGALES
# ============================================
@app.get("/api/documentos/listar")
async def listar_documentos(request: Request, categoria: Optional[str] = None, tipo: Optional[str] = None):
    with get_db() as con:
        query = "SELECT * FROM documentos_legales WHERE activo = 1"
        params = []
        if tipo:
            query += " AND tipo = ?"
            params.append(tipo)
        if categoria:
            query += " AND categoria = ?"
            params.append(categoria)
        query += " ORDER BY orden ASC, nombre ASC"
        docs = con.execute(query, params).fetchall()
    return ok([dict(d) for d in docs])


@app.get("/api/documentos/admin/listar")
async def listar_documentos_admin(request: Request):
    require_login(request)
    with get_db() as con:
        docs = con.execute("SELECT * FROM documentos_legales ORDER BY categoria, orden ASC, nombre ASC").fetchall()
    return ok([dict(d) for d in docs])


@app.post("/api/documentos/crear")
async def crear_documento(
    request: Request,
    tipo: str = Form(...),
    categoria: str = Form(...),
    nombre: str = Form(...),
    descripcion: Optional[str] = Form(None),
    orden: int = Form(0),
    archivo: UploadFile = File(...),
):
    require_login(request)
    if not is_pdf_upload(archivo):
        return fail("Solo se permiten archivos PDF", status_code=400)
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_filename = sanitize_filename(archivo.filename)
    filename = f"doc_{timestamp}_{safe_filename}"
    file_path = DOCUMENTS_DIR / filename
    content = await archivo.read()
    with open(file_path, "wb") as f:
        f.write(content)
    created_at = now_iso()
    with get_db() as con:
        con.execute(
            "INSERT INTO documentos_legales (tipo, categoria, nombre, descripcion, archivo, orden, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tipo, categoria, nombre, descripcion, filename, orden, created_at)
        )
        con.commit()
    return ok(message="Documento creado correctamente")


@app.put("/api/documentos/actualizar/{doc_id}")
async def actualizar_documento(
    doc_id: int,
    request: Request,
    categoria: str = Form(...),
    nombre: str = Form(...),
    descripcion: Optional[str] = Form(None),
    orden: int = Form(0),
    activo: int = Form(1),
    archivo: Optional[UploadFile] = File(None),
):
    require_login(request)
    updated_at = now_iso()
    with get_db() as con:
        doc = con.execute("SELECT * FROM documentos_legales WHERE id = ?", (doc_id,)).fetchone()
        if not doc:
            return fail("Documento no encontrado", status_code=404)
        filename = doc["archivo"]
        if archivo and archivo.filename:
            if not is_pdf_upload(archivo):
                return fail("Solo se permiten archivos PDF", status_code=400)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_filename = sanitize_filename(archivo.filename)
            filename = f"doc_{timestamp}_{safe_filename}"
            file_path = DOCUMENTS_DIR / filename
            content = await archivo.read()
            with open(file_path, "wb") as f:
                f.write(content)
            old_file = DOCUMENTS_DIR / doc["archivo"]
            if old_file.exists():
                old_file.unlink()
        con.execute(
            "UPDATE documentos_legales SET categoria = ?, nombre = ?, descripcion = ?, archivo = ?, orden = ?, activo = ?, updated_at = ? WHERE id = ?",
            (categoria, nombre, descripcion, filename, orden, activo, updated_at, doc_id)
        )
        con.commit()
    return ok(message="Documento actualizado correctamente")


@app.delete("/api/documentos/eliminar/{doc_id}")
async def eliminar_documento(doc_id: int, request: Request):
    require_login(request)
    with get_db() as con:
        doc = con.execute("SELECT * FROM documentos_legales WHERE id = ?", (doc_id,)).fetchone()
        if not doc:
            return fail("Documento no encontrado", status_code=404)
        file_path = DOCUMENTS_DIR / doc["archivo"]
        if file_path.exists():
            file_path.unlink()
        con.execute("DELETE FROM documentos_legales WHERE id = ?", (doc_id,))
        con.commit()
    return ok(message="Documento eliminado correctamente")


# ============================================
# 11. API SECCIONES DE TRANSPARENCIA
# ============================================
@app.get("/api/transparencia/secciones")
async def listar_secciones():
    with get_db() as con:
        secciones = con.execute(
            "SELECT * FROM secciones_transparencia WHERE activo = 1 ORDER BY orden ASC, numero ASC"
        ).fetchall()
        resultado = []
        for seccion in secciones:
            subsecciones = con.execute(
                "SELECT * FROM subsecciones_transparencia WHERE seccion_id = ? AND activo = 1 ORDER BY orden ASC",
                (seccion["id"],)
            ).fetchall()
            resultado.append({
                "id": seccion["id"],
                "numero": seccion["numero"],
                "titulo": seccion["titulo"],
                "orden": seccion["orden"],
                "subsecciones": [dict(s) for s in subsecciones]
            })
    return ok(resultado)


@app.get("/api/transparencia/secciones/admin/listar")
async def listar_secciones_admin(request: Request):
    require_login(request)
    with get_db() as con:
        secciones = con.execute(
            "SELECT * FROM secciones_transparencia ORDER BY orden ASC, numero ASC"
        ).fetchall()
        resultado = []
        for seccion in secciones:
            subsecciones = con.execute(
                "SELECT * FROM subsecciones_transparencia WHERE seccion_id = ? ORDER BY orden ASC",
                (seccion["id"],)
            ).fetchall()
            resultado.append({
                "id": seccion["id"],
                "numero": seccion["numero"],
                "titulo": seccion["titulo"],
                "orden": seccion["orden"],
                "activo": seccion["activo"],
                "subsecciones": [dict(s) for s in subsecciones]
            })
    return ok(resultado)


@app.post("/api/transparencia/secciones/crear")
async def crear_seccion(
    request: Request,
    numero: int = Form(...),
    titulo: str = Form(...),
    orden: int = Form(0),
    activo: int = Form(1)
):
    require_login(request)
    created_at = now_iso()
    with get_db() as con:
        con.execute(
            "INSERT INTO secciones_transparencia (numero, titulo, orden, activo, created_at) VALUES (?, ?, ?, ?, ?)",
            (numero, titulo, orden, activo, created_at)
        )
        con.commit()
    return ok(message="Sección creada correctamente")


@app.put("/api/transparencia/secciones/actualizar/{seccion_id}")
async def actualizar_seccion(
    seccion_id: int,
    request: Request,
    numero: int = Form(...),
    titulo: str = Form(...),
    orden: int = Form(0),
    activo: int = Form(1)
):
    require_login(request)
    updated_at = now_iso()
    with get_db() as con:
        con.execute(
            "UPDATE secciones_transparencia SET numero = ?, titulo = ?, orden = ?, activo = ?, updated_at = ? WHERE id = ?",
            (numero, titulo, orden, activo, updated_at, seccion_id)
        )
        con.commit()
    return ok(message="Sección actualizada correctamente")


@app.delete("/api/transparencia/secciones/eliminar/{seccion_id}")
async def eliminar_seccion(seccion_id: int, request: Request):
    require_login(request)
    with get_db() as con:
        con.execute("DELETE FROM secciones_transparencia WHERE id = ?", (seccion_id,))
        con.commit()
    return ok(message="Sección eliminada correctamente")

# ===== SUB SECCIONES CON SOPORTE PARA PDF =====
@app.post("/api/transparencia/subsecciones/crear")
async def crear_subseccion(
    request: Request,
    seccion_id: int = Form(...),
    nombre: str = Form(...),
    tipo_contenido: str = Form("enlace"),
    link: str = Form(""),
    orden: int = Form(0),
    activo: int = Form(1),
    archivo: Optional[UploadFile] = File(None)
):
    require_login(request)
    created_at = now_iso()
    
    filename = None
    if archivo and archivo.filename:
        if not archivo.filename.lower().endswith('.pdf'):
            return fail("Solo se permiten archivos PDF", status_code=400)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_filename = sanitize_filename(archivo.filename)
        filename = f"transparencia_{timestamp}_{safe_filename}"
        file_path = DOCUMENTS_DIR / filename
        content = await archivo.read()
        with open(file_path, "wb") as f:
            f.write(content)
    
    with get_db() as con:
        con.execute(
            """
            INSERT INTO subsecciones_transparencia 
            (seccion_id, nombre, tipo_contenido, link, archivo_pdf, orden, activo, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (seccion_id, nombre, tipo_contenido, link, filename, orden, activo, created_at)
        )
        con.commit()
    
    return ok(message="Subsección creada correctamente")


@app.put("/api/transparencia/subsecciones/actualizar/{subseccion_id}")
async def actualizar_subseccion(
    subseccion_id: int,
    request: Request,
    nombre: str = Form(...),
    tipo_contenido: str = Form("enlace"),
    link: str = Form(""),
    orden: int = Form(0),
    activo: int = Form(1),
    archivo: Optional[UploadFile] = File(None)
):
    require_login(request)
    updated_at = now_iso()
    
    with get_db() as con:
        sub = con.execute("SELECT * FROM subsecciones_transparencia WHERE id = ?", (subseccion_id,)).fetchone()
        if not sub:
            return fail("Subsección no encontrada", status_code=404)
        
        filename = sub["archivo_pdf"]
        
        if archivo and archivo.filename:
            if not archivo.filename.lower().endswith('.pdf'):
                return fail("Solo se permiten archivos PDF", status_code=400)
            
            # Eliminar archivo viejo
            if filename:
                old_file = DOCUMENTS_DIR / filename
                if old_file.exists():
                    old_file.unlink()
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_filename = sanitize_filename(archivo.filename)
            filename = f"transparencia_{timestamp}_{safe_filename}"
            file_path = DOCUMENTS_DIR / filename
            content = await archivo.read()
            with open(file_path, "wb") as f:
                f.write(content)
        
        con.execute(
            """
            UPDATE subsecciones_transparencia 
            SET nombre = ?, tipo_contenido = ?, link = ?, archivo_pdf = ?,
                orden = ?, activo = ?, updated_at = ?
            WHERE id = ?
            """,
            (nombre, tipo_contenido, link, filename, orden, activo, updated_at, subseccion_id)
        )
        con.commit()
    
    return ok(message="Subsección actualizada correctamente")


@app.delete("/api/transparencia/subsecciones/eliminar/{subseccion_id}")
async def eliminar_subseccion(subseccion_id: int, request: Request):
    require_login(request)
    
    with get_db() as con:
        sub = con.execute("SELECT * FROM subsecciones_transparencia WHERE id = ?", (subseccion_id,)).fetchone()
        if sub and sub["archivo_pdf"]:
            file_path = DOCUMENTS_DIR / sub["archivo_pdf"]
            if file_path.exists():
                file_path.unlink()
        
        con.execute("DELETE FROM subsecciones_transparencia WHERE id = ?", (subseccion_id,))
        con.commit()
    
    return ok(message="Subsección eliminada correctamente")

# ============================================
# 11.1 API TRABAJA CON NOSOTROS
# ============================================
@app.post("/api/trabaja-con-nosotros")
async def recibir_hoja_de_vida(
    nombre: str = Form(...),
    email: str = Form(...),
    telefono: str = Form(...),
    cargo: str = Form(...),
    mensaje: Optional[str] = Form(None),
    cv: UploadFile = File(...),
):
    nombre = nombre.strip()
    email = email.strip()
    telefono = telefono.strip()
    cargo = cargo.strip()
    mensaje = mensaje.strip() if mensaje else ""

    if len(nombre) < 3:
        return fail("El nombre debe tener al menos 3 caracteres", status_code=400)

    if len(email) < 6 or "@" not in email:
        return fail("Correo electrónico inválido", status_code=400)

    if len(telefono) < 7:
        return fail("Teléfono inválido", status_code=400)

    if len(cargo) < 2:
        return fail("Debe indicar el cargo al que aspira", status_code=400)

    if not cv or not cv.filename:
        return fail("Debe adjuntar hoja de vida en PDF", status_code=400)

    if not cv.filename.lower().endswith(".pdf"):
        return fail("La hoja de vida debe estar en formato PDF", status_code=400)

    CV_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = sanitize_filename(cv.filename)
    filename = f"cv_{timestamp}_{safe_name}"
    file_path = CV_UPLOADS_DIR / filename

    content = await cv.read()

    if not content:
        return fail("El archivo PDF está vacío", status_code=400)

    with open(file_path, "wb") as f:
        f.write(content)

    print("=" * 60)
    print("📩 NUEVA HOJA DE VIDA RECIBIDA")
    print(f"Nombre: {nombre}")
    print(f"Email: {email}")
    print(f"Teléfono: {telefono}")
    print(f"Cargo: {cargo}")
    print(f"Mensaje: {mensaje}")
    print(f"Archivo: {file_path}")
    print("Enviar a: marca@supergas.com.co")
    print("=" * 60)

    return ok(
        {
            "archivo": filename,
            "destino": "marca@supergas.com.co"
        },
        message="Hoja de vida recibida correctamente"
    )

# ============================================
# 11.2 API NOTICIAS
# ============================================
@app.get("/api/noticias/listar")
def listar_noticias_publicas():
    with get_db() as con:
        noticias = con.execute(
            """
            SELECT *
            FROM noticias
            WHERE activo = 1
            ORDER BY orden ASC, created_at DESC
            """
        ).fetchall()

    return ok([dict(n) for n in noticias])


@app.get("/api/noticias/admin/listar")
def listar_noticias_admin(request: Request):
    require_login(request)

    with get_db() as con:
        noticias = con.execute(
            """
            SELECT *
            FROM noticias
            ORDER BY orden ASC, created_at DESC
            """
        ).fetchall()

    return ok([dict(n) for n in noticias])


@app.post("/api/noticias/crear")
async def crear_noticia(
    request: Request,
    titulo: str = Form(...),
    descripcion: Optional[str] = Form(None),
    tipo: str = Form("instagram"),
    enlace: Optional[str] = Form(None),
    imagen: Optional[str] = Form(None),
    orden: int = Form(0),
    activo: int = Form(1),
):
    require_login(request)

    titulo = titulo.strip()
    descripcion = descripcion.strip() if descripcion else ""
    tipo = tipo.strip()
    enlace = enlace.strip() if enlace else ""
    imagen = imagen.strip() if imagen else ""

    if len(titulo) < 3:
        return fail("El título debe tener al menos 3 caracteres", status_code=400)

    tipos_validos = ["instagram", "interna", "externa"]

    if tipo not in tipos_validos:
        return fail(
            "Tipo de noticia inválido. Usa: instagram, interna o externa",
            status_code=400
        )

    created_at = now_iso()

    with get_db() as con:
        con.execute(
            """
            INSERT INTO noticias
            (titulo, descripcion, tipo, enlace, imagen, orden, activo, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                titulo,
                descripcion,
                tipo,
                enlace,
                imagen,
                orden,
                activo,
                created_at
            )
        )

        con.commit()

    return ok(message="Noticia creada correctamente", status_code=201)


@app.put("/api/noticias/actualizar/{noticia_id}")
async def actualizar_noticia(
    noticia_id: int,
    request: Request,
    titulo: str = Form(...),
    descripcion: Optional[str] = Form(None),
    tipo: str = Form("instagram"),
    enlace: Optional[str] = Form(None),
    imagen: Optional[str] = Form(None),
    orden: int = Form(0),
    activo: int = Form(1),
):
    require_login(request)

    titulo = titulo.strip()
    descripcion = descripcion.strip() if descripcion else ""
    tipo = tipo.strip()
    enlace = enlace.strip() if enlace else ""
    imagen = imagen.strip() if imagen else ""

    if len(titulo) < 3:
        return fail("El título debe tener al menos 3 caracteres", status_code=400)

    tipos_validos = ["instagram", "interna", "externa"]

    if tipo not in tipos_validos:
        return fail(
            "Tipo de noticia inválido. Usa: instagram, interna o externa",
            status_code=400
        )

    updated_at = now_iso()

    with get_db() as con:
        noticia = con.execute(
            "SELECT id FROM noticias WHERE id = ?",
            (noticia_id,)
        ).fetchone()

        if not noticia:
            return fail("Noticia no encontrada", status_code=404)

        con.execute(
            """
            UPDATE noticias
            SET titulo = ?,
                descripcion = ?,
                tipo = ?,
                enlace = ?,
                imagen = ?,
                orden = ?,
                activo = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                titulo,
                descripcion,
                tipo,
                enlace,
                imagen,
                orden,
                activo,
                updated_at,
                noticia_id
            )
        )

        con.commit()

    return ok(message="Noticia actualizada correctamente")


@app.delete("/api/noticias/eliminar/{noticia_id}")
def eliminar_noticia(noticia_id: int, request: Request):
    require_login(request)

    with get_db() as con:
        cursor = con.execute(
            "DELETE FROM noticias WHERE id = ?",
            (noticia_id,)
        )

        con.commit()

    if cursor.rowcount == 0:
        return fail("Noticia no encontrada", status_code=404)

    return ok(message="Noticia eliminada correctamente")

@app.put("/api/noticias/toggle-activo/{noticia_id}")
def toggle_noticia_activa(noticia_id: int, request: Request):
    require_login(request)

    with get_db() as con:

        noticia = con.execute(
            "SELECT id, activo FROM noticias WHERE id = ?",
            (noticia_id,)
        ).fetchone()

        if not noticia:
            return fail("Noticia no encontrada", status_code=404)

        nuevo_estado = 0 if noticia["activo"] == 1 else 1

        con.execute(
            """
            UPDATE noticias
            SET activo = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                nuevo_estado,
                now_iso(),
                noticia_id
            )
        )

        con.commit()

    return ok(
        {
            "activo": nuevo_estado
        },
        message="Estado actualizado correctamente"
    )

# ============================================
# 11.3 API TARIFAS
# ============================================
@app.get("/api/tarifas/listar")
def listar_tarifas_publicas():
    with get_db() as con:
        tarifas = con.execute(
            """
            SELECT *
            FROM tarifas
            WHERE activo = 1
            ORDER BY anio DESC, mes DESC, orden ASC
            """
        ).fetchall()

    return ok([dict(t) for t in tarifas])


@app.get("/api/tarifas/admin/listar")
def listar_tarifas_admin(request: Request):
    require_login(request)

    with get_db() as con:
        tarifas = con.execute(
            """
            SELECT *
            FROM tarifas
            ORDER BY anio DESC, mes DESC, orden ASC
            """
        ).fetchall()

    return ok([dict(t) for t in tarifas])


@app.post("/api/tarifas/crear")
async def crear_tarifa(
    request: Request,
    anio: int = Form(...),
    mes: int = Form(...),
    nombre: str = Form(...),
    descripcion: Optional[str] = Form(None),
    orden: int = Form(0),
    activo: int = Form(1),
    archivo: UploadFile = File(...),
):
    require_login(request)

    nombre = nombre.strip()
    descripcion = descripcion.strip() if descripcion else ""

    if anio < 2020 or anio > 2100:
        return fail("Año inválido", status_code=400)

    if mes < 1 or mes > 12:
        return fail("Mes inválido", status_code=400)

    if len(nombre) < 3:
        return fail("El nombre debe tener al menos 3 caracteres", status_code=400)

    if not is_pdf_upload(archivo):
        return fail("Solo se permiten archivos PDF", status_code=400)

    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_filename = sanitize_filename(archivo.filename)
    filename = f"tarifa_{anio}_{mes:02d}_{timestamp}_{safe_filename}"
    file_path = DOCUMENTS_DIR / filename

    content = await archivo.read()

    if not content:
        return fail("El archivo PDF está vacío", status_code=400)

    with open(file_path, "wb") as f:
        f.write(content)

    created_at = now_iso()

    with get_db() as con:
        con.execute(
            """
            INSERT INTO tarifas
            (anio, mes, nombre, descripcion, archivo, activo, orden, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                anio,
                mes,
                nombre,
                descripcion,
                filename,
                activo,
                orden,
                created_at
            )
        )
        con.commit()

    return ok(message="Tarifa creada correctamente", status_code=201)


@app.put("/api/tarifas/toggle-activo/{tarifa_id}")
def toggle_tarifa_activa(tarifa_id: int, request: Request):
    require_login(request)

    with get_db() as con:
        tarifa = con.execute(
            "SELECT id, activo FROM tarifas WHERE id = ?",
            (tarifa_id,)
        ).fetchone()

        if not tarifa:
            return fail("Tarifa no encontrada", status_code=404)

        nuevo_estado = 0 if tarifa["activo"] == 1 else 1

        con.execute(
            """
            UPDATE tarifas
            SET activo = ?, updated_at = ?
            WHERE id = ?
            """,
            (nuevo_estado, now_iso(), tarifa_id)
        )

        con.commit()

    return ok(
        {"activo": nuevo_estado},
        message="Estado de tarifa actualizado correctamente"
    )


@app.delete("/api/tarifas/eliminar/{tarifa_id}")
def eliminar_tarifa(tarifa_id: int, request: Request):
    require_login(request)

    with get_db() as con:
        tarifa = con.execute(
            "SELECT * FROM tarifas WHERE id = ?",
            (tarifa_id,)
        ).fetchone()

        if not tarifa:
            return fail("Tarifa no encontrada", status_code=404)

        file_path = DOCUMENTS_DIR / tarifa["archivo"]

        if file_path.exists():
            file_path.unlink()

        con.execute(
            "DELETE FROM tarifas WHERE id = ?",
            (tarifa_id,)
        )

        con.commit()

    return ok(message="Tarifa eliminada correctamente")

# ============================================
# 12. HEALTH CHECK
# ============================================
@app.get("/health")
def health_check():
    try:
        with get_db() as con:
            con.execute("SELECT 1").fetchone()
        return {"status": "healthy", "database": "connected", "timestamp": datetime.now().isoformat()}
    except Exception as e:
        return {"status": "unhealthy", "database": "disconnected", "error": str(e), "timestamp": datetime.now().isoformat()}



# Sección 12.1. Montaje final del frontend
app.mount(
    "/",
    StaticFiles(directory=str(FRONTEND_DIR), html=True),
    name="frontend"
)

# ============================================
# 13. EJECUTAR SERVIDOR
# ============================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)

    