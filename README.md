# AWS CLI Credential Manager

A minimal Flask web UI that lets you update AWS CLI credentials (`aws configure set ...`) using browser inputs.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install Flask
python app.py
```

## Usage

Open `http://localhost:8081` (or the port in the `PORT` env var) and fill in:

- `aws_access_key_id`
- `aws_secret_access_key`
- Optional: session token, region (defaults to `us-east-1`), and profile (defaults to `default`).

The page displays the output/exit codes of the `aws configure set` commands.
