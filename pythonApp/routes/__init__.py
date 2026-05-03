from routes.machine_routes import machine_bp
from routes.config_routes import config_bp
from routes.device_routes import device_bp
from routes.access_routes import access_bp
from routes.biometric_routes import biometric_bp


def register_blueprints(app):
    app.register_blueprint(machine_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(device_bp)
    app.register_blueprint(access_bp)
    app.register_blueprint(biometric_bp)
