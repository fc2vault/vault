#!/bin/bash
cd "$(dirname "$0")"
[ -f config.json ] || cp config.example.json config.json
echo "Edit config.json to point 'library' at your movies, then this opens the app."
python3 serve.py &
sleep 2 && (command -v open >/dev/null && open http://127.0.0.1:8730/ || true)
wait
