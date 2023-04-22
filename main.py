from pydantic import BaseModel
from db import db
from typing import TypedDict, Annotated
from fastapi import FastAPI, Depends, HTTPException, Request
import json
import os
import sqlite3
import base64
import bcrypt
import time

TOKEN_EXPIRY = 604800  # 1 week


app = FastAPI()


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    token: str
    expiry: int
    attributes: dict


class RegisterRequest(BaseModel):
    email: str
    password: str


class MeResponse(BaseModel):
    email: str
    attributes: dict


@app.post("/api/login")
def login(request: LoginRequest) -> LoginResponse:
    cur = db.cursor()
    res = cur.execute(
        "SELECT passhash, attributes FROM users WHERE email = ?", (request.email,)
    )

    row = res.fetchone()
    if row is None:
        raise HTTPException(status_code=401)

    if not bcrypt.checkpw(request.password.encode(), row[0].encode()):
        raise HTTPException(status_code=401)

    token = base64.b64encode(os.urandom(32)).decode()
    expire = int(time.time()) + TOKEN_EXPIRY
    cur.execute(
        "INSERT INTO sessions (email, token, expiration) VALUES (?, ?, ?)",
        (request.email, token, expire),
    )
    db.commit()

    return LoginResponse(token=token, expiry=expire, attributes=json.loads(row[1]))


@app.post("/api/register")
def register(request: RegisterRequest):
    passhash = bcrypt.hashpw(request.password.encode(), bcrypt.gensalt()).decode()

    cur = db.cursor()
    cur.execute(
        "INSERT INTO users (email, passhash, attributes) VALUES (?, ?, ?)",
        (request.email, passhash, json.dumps({})),
    )
    db.commit()


class AuthorizedUser:
    email: str

    def __init__(self, email: str):
        self.email = email


def get_authorized_user(request: Request) -> AuthorizedUser:
    unauthorized_paths = ["/api/login", "/api/register"]
    if request.url.path in unauthorized_paths:
        raise ValueError("path doesn't have authorization")

    token = request.headers.get("Authorization")
    if token is None:
        raise HTTPException(status_code=401)

    cur = db.cursor()
    res = cur.execute(
        "SELECT email FROM sessions WHERE token = ? AND expiration > ?",
        (token, int(time.time())),
    )
    row = res.fetchone()
    if row is None:
        raise HTTPException(status_code=401)

    return AuthorizedUser(row[0])


@app.get("/api/me")
def me(user: AuthorizedUser = Depends(get_authorized_user)) -> MeResponse:
    cur = db.cursor()
    res = cur.execute("SELECT attributes FROM users WHERE email = ?", (user.email,))
    row = res.fetchone()
    if row is None:
        raise HTTPException(status_code=500)

    return MeResponse(email=user.email, attributes=json.loads(row[0]))
