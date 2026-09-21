from app import create_app
from app.extensions import db

app = create_app()

with app.app_context():
    db.create_all()
    from app.schema import ensure_schema
    ensure_schema(db)
    import jobs  # noqa: F401  (registers jobs into app.api.JOB_REGISTRY)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
