from fastapi import FastAPI
from pydantic import BaseModel
from google import genai
import base64

app = FastAPI()

class Imgdata(BaseModel):
    image: str

@app.get("/")
def homepage():
    return "Do not access this page normally"

@app.post("/sendimg/")
def getimage(gimg: Imgdata):
    return {"message": "Image received"}
