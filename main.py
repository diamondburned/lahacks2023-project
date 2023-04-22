import json
import os
import base64
import bcrypt
import time

from dotenv import load_dotenv
from requests import request
from fastapi import FastAPI, Depends, HTTPException, Request, Query
from snowflake import SnowflakeGenerator

from airports import (
    find_by_name as find_airports_by_name,
    find_by_coords as find_airports_by_coords,
)

from db import db
from models import *

load_dotenv()

MAX_WAIT = 5000
MIN_WAIT = 500

TOKEN_EXPIRY = 604800  # 1 week


app = FastAPI(
    docs_url="/api/docs", redoc_url="/api/redoc", openapi_url="/api/openapi.json"
)
id_generator = SnowflakeGenerator(0)


@app.get("/api/ping")
def ping():
    return "Pong!!!"


@app.post("/api/login")
def login(request: LoginRequest) -> LoginResponse:
    cur = db.cursor()
    res = cur.execute(
        "SELECT id, passhash, first_name, profile_picture FROM users WHERE email = ?", (request.email,)
    )

    row = res.fetchone()
    if row is None:
        raise HTTPException(status_code=401)

    if not bcrypt.checkpw(request.password.encode(), row[1].encode()):
        raise HTTPException(status_code=401)

    token = base64.b64encode(os.urandom(32)).decode()
    expire = int(time.time()) + TOKEN_EXPIRY

    cur.execute(
        "INSERT INTO sessions (token, user_id, expiration) VALUES (?, ?, ?)",
        (token, row[0], expire),
    )
    db.commit()

    return LoginResponse(
        id=row[0],
        first_name=row[2],
        profile_picture=row[3],
        token=token,
        expiry=expire,
    )


@app.post("/api/register", status_code=204)
def register(request: RegisterRequest):
    id = str(next(id_generator))
    passhash = bcrypt.hashpw(request.password.encode(), bcrypt.gensalt()).decode()

    cur = db.cursor()
    cur.execute(
        "INSERT INTO users (id, email, first_name, passhash) VALUES (?, ?, ?, ?)",
        (id, request.email, request.first_name, passhash),
    )
    db.commit()


class AuthorizedUser:
    id: str

    def __init__(self, id: str):
        self.id = id


def get_authorized_user(request: Request) -> AuthorizedUser:
    unauthorized_paths = ["/api/login", "/api/register"]
    if request.url.path in unauthorized_paths:
        raise ValueError("path doesn't have authorization")

    token = request.headers.get("Authorization")
    if token is None:
        raise HTTPException(status_code=401)

    cur = db.cursor()
    res = cur.execute(
        "SELECT user_id FROM sessions WHERE token = ? AND expiration > ?",
        (token, int(time.time())),
    )
    row = res.fetchone()
    if row is None:
        raise HTTPException(status_code=401)

    return AuthorizedUser(row[0])


@app.get("/api/me")
def me(user: AuthorizedUser = Depends(get_authorized_user)) -> MeResponse:
    cur = db.cursor()
    res = cur.execute(
        "SELECT id, email, first_name, profile_picture FROM users WHERE id = ?",
        (user.id,),
    )
    row = res.fetchone()
    if row is None:
        raise HTTPException(status_code=500)

    return MeResponse(**row)


@app.get("/api/flights")
def get_flights(
    date: str = Query(description="date of flight in YYYYMMDD format"),
    origin: str = Query(description="3-letter airport code (IATA)"),
    dest: str = Query(description="3-letter airport code (IATA)"),
    num_adults: int | None = Query(1, description="number of adults"),
    wait_time: int | None = Query(None, description="max wait time in minutes"),
    # page: int = Query(1, description="page number"),
    # TODO: Round trip 
) -> FlightResponse:
    # TODO: implement eviction for old cached flights
    PAGE_SIZE = 50
    resp: FlightResponse

    cur = db.cursor()
    res = cur.execute(
        "SELECT response FROM flight_responses WHERE date = ? AND origin = ? AND destination = ?",
        (date, origin, dest),
    )

    row = res.fetchone()
    if row is not None:
        resp = FlightResponse.parse_raw(row[0])
    else:
        host = "skyscanner50.p.rapidapi.com"
        url = "https://" + host + "/api/v1/searchFlightsMultiStops"

        query_string = {
            "legs": json.dumps(
                [
                    {
                        "origin": origin,
                        "destination": dest,
                        "date": date,
                    }
                ]
            ),
            "waitTime": min(wait_time, MAX_WAIT) if wait_time is not None else MIN_WAIT,
            "adults": num_adults,
            "currency": "USD",
            "countryCode": "US",
            "market": "en-US",
        }

        headers = {
            "X-RapidAPI-Key": os.getenv("RAPID_API_KEY"),
            "X-RapidAPI-Host": host,
        }

        res = request("GET", url, headers=headers, params=query_string)

        parsed_res = FlightResponse.parse_raw(res.text)

        if parsed_res is None or parsed_res.data is None:
            return parsed_res

        to_delete = []

        for flight in parsed_res.data:
            if flight.legs is None:
                to_delete.append(flight)
                continue

            for leg in flight.legs:
                if leg.stops is None or len(leg.stops) == 0:
                    to_delete.append(flight)

        for flight in to_delete:
            parsed_res.data.remove(flight)

        resp = parsed_res

        cur.execute(
            "INSERT INTO flight_responses (date, origin, destination, timestamp, response) VALUES (?, ?, ?, ?)",
            (
                date,
                origin,
                dest,
                resp.timestamp or int(time.time() * 1000),
                resp.json(),
            ),
        )
        db.commit()

    # if resp.data is not None:
    #     start = (page - 1) * PAGE_SIZE
    #     end = start + PAGE_SIZE
    #     resp.data = resp.data[start:end]

    return resp


@app.get("/api/airports")
def airports(
    name: str = Query(None, description="airport name (must not have lat or long)"),
    lat: float = Query(None, description="latitude (must also have long)"),
    long: float = Query(None, description="longitude (must also have lat)"),
) -> ListAirportsResponse:
    if name:
        airports = find_airports_by_name(name)
    elif lat and long:
        airports = find_airports_by_coords(lat, long)
    else:
        raise HTTPException(status_code=400, detail="need either ?name or ?lat&long")

    return ListAirportsResponse(airports=airports)
