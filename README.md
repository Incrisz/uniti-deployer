# Uniti Deployment Helper

This Flask web app exposes a `/deploy` route that lets you trigger a service update from your browser. Confirming the prompt runs the bundled `deploy_pipeline.sh` script, which handles Docker Compose shutdown/start and `git pull` inside `/root/uniti-model-service`, streaming command output plus Git metadata back to the page.

## Setup
- Install dependencies: `pip install -r requirements.txt`
- Run the server locally: `python app.py`
- Or build and run the container: `docker build -t uniti-deployer .` then `docker run --rm -p 5000:5000 -v /root/uniti-model-service:/root/uniti-model-service -v /var/run/docker.sock:/var/run/docker.sock -v /usr/bin/docker:/usr/bin/docker:ro -v /usr/lib/docker/cli-plugins:/usr/lib/docker/cli-plugins:ro uniti-deployer`
- Or launch with Docker Compose: `docker compose up --build`

If you run the helper in Docker, make sure the container can reach the Docker CLI **and** the Compose plugin. The example above bind-mounts `/usr/bin/docker` plus `/usr/lib/docker/cli-plugins`, which is where Ubuntu installs the v2 plugin—adjust the paths if your host uses different locations.

Expose the host’s port 5000 publicly only if your firewall/network rules allow it.
Ensure `deploy_pipeline.sh` is executable (`chmod +x deploy_pipeline.sh`), and that `git` plus either `docker` (with the compose plugin) or `docker-compose` are installed and on the PATH for whichever user runs the app.

## File Overview
- `app.py` – Flask app with the landing page and `/deploy` route plus command execution logic.
- `deploy_pipeline.sh` – Bash script invoked by the app to stop containers, pull updates, and restart the service.
- `templates/index.html` – Home page with a button that links to the deployment form.
- `templates/deploy_form.html` – Confirmation form shown before deployment.
- `templates/deploy_result.html` – Displays command logs and Git summary after execution.
- `requirements.txt` – Python dependencies (Flask).
- `Dockerfile` – Container definition running the app on port 5000 (installs git for repo metadata).
- `docker-compose.yml` – Compose service exposing port 5000, mounting the model service repo, Docker socket, and Docker CLI/compose plugin from the host.
- `.dockerignore` – Files excluded from Docker build context.
