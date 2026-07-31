# artifacts — static HTML sharing

Serve `/workspace/html/<slug>/index.html` su `https://artifacts.casita.monster/<slug>/`
(dietro VPN, noindex). Gestito dalla skill `html-share`.

- Container: `nginx:alpine`, stack Portainer `artifacts` (endpoint macpro, id 353).
- Root montata in **sola lettura**: `~/.../workspace/html`.
- La cartella `html/` è nel workspace, quindi coperta dal backup Kopia (`/data/tg-gp23`).

Restart: `python3 /workspace/.claude/skills/portainer/portainer.py container-restart artifacts`
