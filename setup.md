

## run in the background
nohup gunicorn -w 2 -b 0.0.0.0:8081 app:app >/var/log/uniti-cron.log 2>&1 &

## generate key
ssh-keygen -t ed25519 -C "signals-repo" -f ~/.ssh/id_signals

## set key 
cd /path/to/signals
git config core.sshCommand "ssh -i ~/.ssh/id_signals -o IdentitiesOnly=yes"

## You can verify with:
git config --get core.sshCommand


## to test
ssh -T git@bitbucket.org
