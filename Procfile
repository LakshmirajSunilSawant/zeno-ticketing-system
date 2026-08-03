# WHY also a Procfile: render.yaml is the source of truth, but a Procfile makes the same app
# deployable to Railway/Heroku/Fly without editing anything. $PORT is supplied by the platform.
web: python seed.py && uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1
