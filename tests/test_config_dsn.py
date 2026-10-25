"""Database settings retain their literal values when libpq parses the URI."""

import pytest
from psycopg.conninfo import conninfo_to_dict

from bce.config import Settings


@pytest.mark.parametrize("field", ["user", "password", "name"])
@pytest.mark.parametrize("value", ["p@ss/word", "literal%40", "with space", "a:b?#&=", "pässwörd"])
def test_dsn_preserves_reserved_characters(field: str, value: str) -> None:
    values = {"db_user": "bce", "db_password": "bce", "db_name": "bce"}
    values[f"db_{field}"] = value
    settings = Settings(_env_file=None, db_host="localhost", db_port=5432, **values)

    assert conninfo_to_dict(settings.dsn) == {
        "user": values["db_user"],
        "password": values["db_password"],
        "dbname": values["db_name"],
        "host": "localhost",
        "port": "5432",
    }


def test_dsn_keeps_plain_credentials_unchanged() -> None:
    settings = Settings(
        _env_file=None,
        db_user="bce",
        db_password="bce",
        db_name="bce",
        db_host="localhost",
        db_port=5432,
    )

    assert settings.dsn == "postgresql://bce:bce@localhost:5432/bce"
