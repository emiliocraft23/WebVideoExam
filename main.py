import os
import uuid
import shutil
import bcrypt
import boto3
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Text, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# ---------------------------------------------------------
# CONFIGURACIÓN DE AWS S3
# ---------------------------------------------------------
load_dotenv()

s3_client = boto3.client(
    's3',
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
    region_name=os.getenv('AWS_REGION', 'us-east-1')
)
BUCKET_NAME = os.getenv('AWS_BUCKET_NAME')

def upload_to_s3(file_obj, filename, folder=""):
    safe_filename = filename.replace(" ", "_")
    unique_name = f"{folder}/{uuid.uuid4()}_{safe_filename}"
    
    s3_client.upload_fileobj(
        file_obj.file,
        BUCKET_NAME,
        unique_name,
        ExtraArgs={"ContentType": file_obj.content_type}
    )
    
    region = os.getenv('AWS_REGION', 'us-east-1')
    return f"https://{BUCKET_NAME}.s3.{region}.amazonaws.com/{unique_name}"

# ---------------------------------------------------------
# CONFIGURACIÓN DE BASE DE DATOS (Producción: AWS RDS PostgreSQL)
# ---------------------------------------------------------
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")

SQLALCHEMY_DATABASE_URL = f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:5432/{DB_NAME}"

# Al usar PostgreSQL ya no necesitamos el "check_same_thread" de SQLite
engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50))
    email = Column(String(50), unique=True, index=True)
    password_hash = Column(String(100))
    avatar_url = Column(String(255), nullable=True, default="https://tu-bucket.s3.com/default-avatar.png")
    channel_description = Column(Text, nullable=True) 

class Video(Base):
    __tablename__ = "videos"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(100))
    description = Column(Text)
    video_url = Column(String(255))
    thumbnail_url = Column(String(255))
    user_id = Column(Integer, ForeignKey("users.id"))

Base.metadata.create_all(bind=engine)

# ---------------------------------------------------------
# CONFIGURACIÓN DE FASTAPI Y SEGURIDAD
# ---------------------------------------------------------
app = FastAPI(title="Video Platform API - Integración S3")

app = FastAPI()

# Configuración de CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # El asterisco es la clave para permitir conexiones externas
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------------------------------------------------------
# ENDPOINTS (RUTAS)
# ---------------------------------------------------------
@app.post("/users")
def create_user(
    name: str = Form(...), 
    email: str = Form(...), 
    password: str = Form(...), 
    channel_description: str = Form(None), 
    avatar_file: UploadFile = File(None),  
    db: Session = Depends(get_db)
):
    salt = bcrypt.gensalt()
    hashed_password = bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
    
    # Subida REAL del avatar a AWS S3
    avatar_url = None
    if avatar_file:
        avatar_url = upload_to_s3(avatar_file, avatar_file.filename, "avatars")
    
    new_user = User(
        name=name, 
        email=email, 
        password_hash=hashed_password,
        avatar_url=avatar_url,
        channel_description=channel_description
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return {"message": "Canal creado exitosamente", "user_id": new_user.id}

@app.post("/login")
def login(email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user or not bcrypt.checkpw(password.encode('utf-8'), user.password_hash.encode('utf-8')):
        raise HTTPException(status_code=400, detail="Credenciales incorrectas")
    return {
        "access_token": f"jwt_{user.id}_token_authenticated",
        "user_id": user.id,
        "name": user.name,
        "email": user.email,
        "avatar_url": user.avatar_url
    }

@app.get("/videos")
def get_videos(db: Session = Depends(get_db)):
    videos = db.query(Video).all()
    result = []
    for v in videos:
        author = db.query(User).filter(User.id == v.user_id).first()
        result.append({
            "id": v.id,
            "title": v.title,
            "description": v.description,
            "video_url": v.video_url,
            "thumbnail_url": v.thumbnail_url,
            "user_id": v.user_id,
            "author_name": author.name if author else "Creador",
            "author_avatar": author.avatar_url if author else None
        })
    return result

@app.post("/videos")
def upload_video(
    title: str = Form(...),
    description: str = Form(...),
    user_id: int = Form(...), 
    video_file: UploadFile = File(...),
    thumbnail_file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    # Subida REAL a AWS S3
    video_url = upload_to_s3(video_file, video_file.filename, "videos")
    thumbnail_url = upload_to_s3(thumbnail_file, thumbnail_file.filename, "thumbnails")

    # Guardamos las URL reales en la base de datos
    new_video = Video(
        title=title, 
        description=description, 
        video_url=video_url, 
        thumbnail_url=thumbnail_url,
        user_id=user_id 
    )
    db.add(new_video)
    db.commit()
    db.refresh(new_video)
    return {"message": "Video publicado con éxito", "video": new_video}

@app.get("/videos/{video_id}")
def get_video(video_id: int, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video no encontrado")
    author = db.query(User).filter(User.id == video.user_id).first()
    return {
        "id": video.id,
        "title": video.title,
        "description": video.description,
        "video_url": video.video_url,
        "thumbnail_url": video.thumbnail_url,
        "user_id": video.user_id,
        "author_name": author.name if author else "Canal desconocido",
        "author_avatar": author.avatar_url if author else None,
        "author_desc": author.channel_description if author else None
    }

@app.get("/channel/{user_id}")
def get_channel(user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Canal no encontrado")
    
    videos = db.query(Video).filter(Video.user_id == user.id).all()
    
    return {
        "channel_info": {
            "name": user.name,
            "avatar_url": user.avatar_url,
            "description": user.channel_description
        },
        "videos": videos
    }