import logging
import os
import pickle
import traceback
from dataclasses import dataclass
from time import sleep
from typing import Optional, List, Callable

from arrow import Arrow
from pytimeparse import parse as parse_seconds
from sqlalchemy import (
    create_engine,
    MetaData,
    Column,
    Table,
    LargeBinary,
    TIMESTAMP,
    INTEGER,
    desc,
)
from sqlalchemy.engine import Engine, Connection
from sqlalchemy.exc import SQLAlchemyError
from withings_api import Credentials2, WithingsApi, MeasureGetMeasResponse, WithingsAuth
from withings_api.common import (
    MeasureGetMeasGroup,
    MeasureType,
    AuthFailedException,
    AuthScope,
)


@dataclass(frozen=True)
class Config:
    client_id: str
    secret: str
    conn_string: str
    refresh_seconds: int

    @staticmethod
    def parse(env=os.environ):
        return Config(
            client_id=env["WITHINGS_CLIENT_ID"],
            secret=env["WITHINGS_SECRET"],
            conn_string=env["SQLALCHEMY_CONN_STRING"],
            refresh_seconds=parse_seconds(env["REFRESH_PERIOD"]),
        )


@dataclass(frozen=True)
class Database:
    engine: Engine
    meta: MetaData
    credentials: Table
    weight: Table


@dataclass
class Weight:
    """Weight measures are in thousandths of a kg, others are in hundredths"""

    created_at: Arrow
    weight: int
    fat_mass: Optional[int]
    muscle_mass: Optional[int]
    hydration: Optional[int]
    bone_mass: Optional[int]
    fat_ratio: Optional[int]
    fat_free_mass: Optional[int]

    @staticmethod
    def from_measure(measure_group: MeasureGetMeasGroup) -> "Weight":
        weight = Weight(
            created_at=measure_group.created,
            weight=-1,
            fat_mass=None,
            muscle_mass=None,
            hydration=None,
            bone_mass=None,
            fat_ratio=None,
            fat_free_mass=None,
        )

        for measure in measure_group.measures:
            if measure.type == MeasureType.WEIGHT:
                weight.weight = measure.value
            elif measure.type == MeasureType.FAT_MASS_WEIGHT:
                weight.fat_mass = measure.value
            elif measure.type == MeasureType.MUSCLE_MASS:
                weight.muscle_mass = measure.value
            elif measure.type == MeasureType.HYDRATION:
                weight.hydration = measure.value
            elif measure.type == MeasureType.BONE_MASS:
                weight.bone_mass = measure.value
            elif measure.type == MeasureType.FAT_RATIO:
                weight.fat_ratio = measure.value
            elif measure.type == MeasureType.FAT_FREE_MASS:
                weight.fat_free_mass = measure.value

        if weight.weight < 0:
            logging.error(f"Failed trying to parse measure, no WEIGHT: {measure_group}")
        return weight


def measures_to_weights(measures: MeasureGetMeasResponse) -> List[Weight]:
    weights = {}
    for measure_group in measures.measuregrps:
        weight = Weight.from_measure(measure_group)
        weights[weight.created_at] = weight
    return sorted(weights.values(), key=lambda w: w.created_at)


def connect_to_database(config: Config) -> Database:
    logging.debug("Connecting to database")
    engine = create_engine(config.conn_string)
    meta = MetaData()

    credentials = Table(
        "credentials", meta, Column("credentials_pkl", LargeBinary, nullable=False)
    )

    weight = Table(
        "weight",
        meta,
        Column("created_at", TIMESTAMP, primary_key=True),
        Column("weight", INTEGER, nullable=False),
        Column("fat_mass", INTEGER),
        Column("muscle_mass", INTEGER),
        Column("hydration", INTEGER),
        Column("bone_mass", INTEGER),
        Column("fat_ratio", INTEGER),
        Column("fat_free_mass", INTEGER),
    )

    meta.create_all(engine)
    return Database(engine, meta, credentials, weight)


def ensure_credentials(conf: Config, database: Database) -> Credentials2:
    """Fetches credentials from the db, and runs oauth flow if they can't be used or found"""
    logging.debug("Attempting to fetch credentials from the database")
    conn = database.engine.connect()

    existing_creds = get_credentials(database, conn)
    if existing_creds:
        try:
            WithingsApi(existing_creds).measure_get_meas()
            return existing_creds
        except AuthFailedException:
            logging.warning("Withings authorization failed, attempting to refresh...")

    auth = WithingsAuth(
        client_id=conf.client_id,
        consumer_secret=conf.secret,
        callback_uri="https://dev.null.test",
        scope=(
            AuthScope.USER_ACTIVITY,
            AuthScope.USER_METRICS,
            AuthScope.USER_INFO,
            AuthScope.USER_SLEEP_EVENTS,
        ),
    )
    auth_url = auth.get_authorize_url()
    print(f"""

Go to the following URL, click the link, and paste the resulting `code` parameter:
{auth_url} 

""")
    code = input("URL Code:")
    creds = auth.get_credentials(code)
    save_credentials(database, conn, creds)
    return creds


def get_credentials(
        database: Database, conn: Optional[Connection]
) -> Optional[Credentials2]:
    if conn is None:
        conn = database.engine.connect()

    last_cred = conn.execute(database.credentials.select()).first()

    if last_cred is not None:
        return deseralize_credentials(last_cred["credentials_pkl"])


def save_credentials(database: Database, conn: Connection, creds: Credentials2):
    conn.execute(database.credentials.delete())
    conn.execute(database.credentials.insert().values(credentials_pkl=serialize_credentials(creds)))


def build_token_refresh_callback(database: Database) -> Callable[[Credentials2], None]:
    conn = database.engine.connect()

    def refresh_callback(creds: Credentials2):
        """Saves credentials when they are refreshed"""
        save_credentials(database, conn, creds)

    return refresh_callback


def monitor_weight(conf: Config, database: Database, creds: Credentials2):
    """Pull weight since last observed weight continuously"""
    logging.debug("Creating auth from credentials")
    refresh_cb = build_token_refresh_callback(database)
    withings = WithingsApi(creds, refresh_cb=refresh_cb)
    if conf.conn_string.startswith('postgresql'):
        from sqlalchemy.dialects.postgresql import insert
    elif 'sqlite' in conf.conn_string:
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy import insert

    while True:
        start_time = Arrow.now()
        conn = database.engine.connect()
        last_weight_timestamp = get_last_weight_timestamp(conn, database)
        logging.info(f"Pulling weights since {last_weight_timestamp}")
        measures = withings.measure_get_meas(
            startdate=last_weight_timestamp, lastupdate=None
        )
        weights = measures_to_weights(measures)

        logging.info(f"Found {len(weights)} new weights")

        with conn.begin():
            try:
                data = [
                    dict(
                        created_at=weight.created_at.datetime,
                        weight=weight.weight,
                        fat_mass=weight.fat_mass,
                        muscle_mass=weight.muscle_mass,
                        hydration=weight.hydration,
                        bone_mass=weight.bone_mass,
                        fat_ratio=weight.fat_ratio,
                        fat_free_mass=weight.fat_free_mass,
                    )
                    for weight in weights
                ]
                insert_stmt = insert(database.weight).values(data).on_conflict_do_nothing()
                results = conn.execute(insert_stmt)
                logging.info(f"Inserted {results.rowcount} rows")
            except SQLAlchemyError:
                logging.error("Failed to write weight to database")
                traceback.print_exc()

        sleep_for = conf.refresh_seconds - (Arrow.now() - start_time).seconds
        logging.debug(f"Sleeping for {sleep_for} seconds")
        sleep(sleep_for)


def get_last_weight_timestamp(conn: Connection, database: Database) -> Optional[Arrow]:
    weights = (
        database.weight.select().order_by(desc(database.weight.c.created_at)).limit(1)
    )
    last_weight = conn.execute(weights).first()
    if last_weight is not None:
        return Arrow.fromdatetime(last_weight.created_at)
    else:
        return None


def serialize_credentials(creds: Credentials2) -> bytes:
    """Serializes credentials using pickle"""
    return pickle.dumps(creds)


def deseralize_credentials(data: bytes) -> Credentials2:
    """Unpickles credentials"""
    return pickle.loads(data)


def main():
    logging.basicConfig(
        format="%(levelname)s:%(asctime)s.%(msecs)03d [%(threadName)s] - %(message)s",
        datefmt="%Y-%m-%d,%H:%M:%S",
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    )
    conf = Config.parse()
    database = connect_to_database(conf)
    credentials = ensure_credentials(conf, database)
    monitor_weight(conf, database, credentials)


if __name__ == "__main__":
    main()
