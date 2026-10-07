import pathlib

import pytest
from flask import Flask

from src.models.base import db


@pytest.fixture
def app():
    template_folder = str(pathlib.Path(__file__).parent / 'src' / 'templates')
    app = Flask(__name__, template_folder=template_folder)
    app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)

    from src.routes import localstack
    app.register_blueprint(localstack)

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()
