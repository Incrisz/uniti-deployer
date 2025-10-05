# Uniti Deployment Helper

This Flask web app exposes a `/deploy` route that lets you trigger a service update from your browser. Confirming the prompt executes `make down`, `git pull`, and `make up` inside `/root/uniti-model-service` and streams command output plus Git metadata back to the page.

## Setup
- Install dependencies: `pip install -r requirements.txt`
- Run the server locally: `python app.py`
- Or build and run the container: `docker build -t uniti-deployer .` then `docker run --rm -p 5000:5000 -v /root/uniti-model-service:/root/uniti-model-service uniti-deployer`
- Or launch with Docker Compose: `docker compose up --build`

## File Overview
- `app.py` – Flask app with the `/deploy` route and command execution logic.
- `templates/deploy_form.html` – Confirmation form shown before deployment.
- `templates/deploy_result.html` – Displays command logs and Git summary after execution.
- `requirements.txt` – Python dependencies (Flask).
- `Dockerfile` – Container definition running the app on port 5000.
- `docker-compose.yml` – Compose service exposing port 5000 and mounting `/root/uniti-model-service`.
- `.dockerignore` – Files excluded from Docker build context.
# uniti-deployer
